"""Profiles and settings of the terminal, kept as JSON in the configuration directory (no Qt).

``settings.json`` holds the settings of the application and a list of
profiles. Everything read from the file is checked: unknown keys are
dropped, values of the wrong type or out of range fall back to their
default, sizes are capped. A file that is not JSON at all is reported and
left alone until the user changes a setting.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from . import ptyhost, schemes
from .emulator import MAX_SCROLLBACK

FILE_NAME = "settings.json"
VERSION = 1
CURSOR_SHAPES = ("bar", "block", "underline")
BELLS = ("visual", "none")
CLOSE_ON_EXIT = ("graceful", "always", "never")
FONT_SIZE_MIN, FONT_SIZE_MAX, FONT_SIZE_DEFAULT = 6.0, 72.0, 11.0
SCROLLBACK_DEFAULT = 10000
FONT_CHOICES = ("Hack", "DejaVu Sans Mono", "Liberation Mono", "monospace")
MAX_PROFILES = 64
MAX_NAME = 64
MAX_COMMAND = 4096
MAX_FILE = 1 << 20
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass
class Profile:
    """How a tab is started and how it looks."""

    id: str
    name: str
    command: str = ""                 # empty: the user's login shell
    directory: str = ""               # empty: where the terminal was started (the home directory from the desktop)
    font_family: str = ""             # empty: the first installed of FONT_CHOICES
    font_size: float = FONT_SIZE_DEFAULT
    scheme: str = schemes.DEFAULT_SCHEME
    cursor_shape: str = "bar"
    cursor_blink: bool = True
    scrollback: int = SCROLLBACK_DEFAULT
    bell: str = "visual"
    close_on_exit: str = "graceful"   # graceful: close when the program ended with 0; always; never


@dataclass
class Settings:
    profiles: list[Profile] = field(default_factory=list)
    default_profile: str = ""
    copy_on_select: bool = False
    paste_warning: bool = True        # ask before pasting text of several lines
    clipboard_write: bool = False     # programs may put text on the clipboard (OSC 52)
    ctrl_c_copies: bool = True        # Ctrl+C copies when text is selected
    confirm_close: bool = True        # ask before closing a tab in which a program is running

    def profile(self, key: str | None) -> Profile:
        """The profile with this ID, else the default profile."""
        for profile in self.profiles:
            if profile.id == key:
                return profile
        return self.default()

    def default(self) -> Profile:
        for profile in self.profiles:
            if profile.id == self.default_profile:
                return profile
        return self.profiles[0]

    def find(self, name: str) -> Profile | None:
        """A profile by ID or by name (case does not matter for the name)."""
        for profile in self.profiles:
            if profile.id == name:
                return profile
        folded = name.casefold()
        for profile in self.profiles:
            if profile.name.casefold() == folded:
                return profile
        return None

    def copy(self) -> "Settings":
        return replace(self, profiles=[replace(profile) for profile in self.profiles])


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def shell_profile_name(shell: str | None = None) -> str:
    """A name for the profile of the login shell: "Bash", "Zsh", ..."""
    name = os.path.basename(shell or ptyhost.default_shell())
    name = _UNSAFE.sub("", name)[:MAX_NAME]
    return name[:1].upper() + name[1:] if name else "Shell"


def defaults() -> Settings:
    profile = Profile("default", shell_profile_name())
    return Settings([profile], profile.id)


def _text(value: object, limit: int, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    return _UNSAFE.sub("", value)[:limit]


def _flag(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _number(value: object, low: float, high: float, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return default
    return min(max(float(value), low), high)


def _choice(value: object, choices: tuple[str, ...], default: str) -> str:
    return value if isinstance(value, str) and value in choices else default


def profile_from(data: object, taken: set[str]) -> Profile | None:
    """A profile from untrusted data, or None when it is not one."""
    if not isinstance(data, dict):
        return None
    key = data.get("id")
    if not isinstance(key, str) or not _ID.match(key) or key in taken:
        key = new_id()
    name = _text(data.get("name"), MAX_NAME).strip() or "Profile"
    scheme = data.get("scheme")
    return Profile(
        id=key, name=name,
        command=_text(data.get("command"), MAX_COMMAND).strip(),
        directory=_text(data.get("directory"), 4096).strip(),
        font_family=_text(data.get("font_family"), 128).strip(),
        font_size=round(_number(data.get("font_size"), FONT_SIZE_MIN, FONT_SIZE_MAX, FONT_SIZE_DEFAULT) * 2) / 2,
        scheme=scheme if isinstance(scheme, str) and schemes.is_known(scheme) else schemes.DEFAULT_SCHEME,
        cursor_shape=_choice(data.get("cursor_shape"), CURSOR_SHAPES, "bar"),
        cursor_blink=_flag(data.get("cursor_blink"), True),
        scrollback=int(_number(data.get("scrollback"), 0, MAX_SCROLLBACK, SCROLLBACK_DEFAULT)),
        bell=_choice(data.get("bell"), BELLS, "visual"),
        close_on_exit=_choice(data.get("close_on_exit"), CLOSE_ON_EXIT, "graceful"),
    )


def from_data(data: object) -> Settings:
    """Settings from untrusted data: what is missing or wrong takes its default."""
    base = defaults()
    if not isinstance(data, dict):
        return base
    profiles: list[Profile] = []
    taken: set[str] = set()
    raw = data.get("profiles")
    for item in raw[:MAX_PROFILES] if isinstance(raw, list) else []:
        profile = profile_from(item, taken)
        if profile is not None:
            profiles.append(profile)
            taken.add(profile.id)
    if not profiles:
        profiles = base.profiles
    default_profile = data.get("default_profile")
    if not isinstance(default_profile, str) or default_profile not in {profile.id for profile in profiles}:
        default_profile = profiles[0].id
    return Settings(
        profiles=profiles, default_profile=default_profile,
        copy_on_select=_flag(data.get("copy_on_select"), base.copy_on_select),
        paste_warning=_flag(data.get("paste_warning"), base.paste_warning),
        clipboard_write=_flag(data.get("clipboard_write"), base.clipboard_write),
        ctrl_c_copies=_flag(data.get("ctrl_c_copies"), base.ctrl_c_copies),
        confirm_close=_flag(data.get("confirm_close"), base.confirm_close),
    )


def to_data(settings: Settings) -> dict:
    data = {item.name: getattr(settings, item.name) for item in fields(Settings) if item.name != "profiles"}
    data["version"] = VERSION
    data["profiles"] = [asdict(profile) for profile in settings.profiles]
    return data


def load(path: Path) -> tuple[Settings, str | None]:
    """Read the settings file. Returns the settings and, when the file could not be used, why (for the user)."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE + 1)
    except FileNotFoundError:
        return defaults(), None
    except OSError as error:
        return defaults(), f"The settings could not be read ({error.strerror or error}). The defaults are used."
    if len(raw) > MAX_FILE:
        return defaults(), "The settings file is too large. The defaults are used."
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return defaults(), "The settings file is damaged. The defaults are used until you change a setting."
    if not isinstance(data, dict):
        return defaults(), "The settings file is damaged. The defaults are used until you change a setting."
    return from_data(data), None


def save(path: Path, settings: Settings) -> None:
    """Write the settings file (through a temporary file, so a crash never leaves half a file)."""
    text = json.dumps(to_data(settings), indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class Launch:
    """What a tab runs: the program, its arguments, where, and what to call it."""

    program: str
    argv: tuple[str, ...]
    directory: str
    name: str


def launch_for(profile: Profile, *, directory: str = "", fallback_directory: str = "",
               command: list[str] | None = None, environ: dict | None = None) -> Launch:
    """What to start for a tab of ``profile``.

    ``command`` (from the command line) replaces the profile's command. The
    tab starts in ``directory`` if given, else in the profile's starting
    directory, else in ``fallback_directory``, else at home. Raises
    ``ptyhost.SpawnError`` when the command cannot be run.
    """
    environ = os.environ if environ is None else environ
    start = ptyhost.usable_directory(directory, os.path.expanduser(profile.directory) if profile.directory else None,
                                     fallback_directory)
    argv = list(command) if command else (ptyhost.split_command(profile.command) if profile.command else [])
    if not argv:
        shell = ptyhost.default_shell(environ)
        program, arguments = ptyhost.shell_command(shell)
        return Launch(program, tuple(arguments), start, os.path.basename(shell))
    program = ptyhost.resolve_program(argv[0], start, dict(environ))
    return Launch(program, tuple(argv), start, os.path.basename(argv[0]))
