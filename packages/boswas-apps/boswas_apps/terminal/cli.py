"""The command line of boswas-terminal (no Qt).

    boswas-terminal [--working-directory DIR] [--title T] [--profile NAME]
                    [--new-window] [--separate] [-e COMMAND [ARGS...]]

Everything after ``-e`` is the command and its arguments, as terminal
emulators conventionally take it. A request is passed to the instance that
is already running as JSON in ``--boswas-request=`` arguments (pieces small
enough for the single-instance channel of the toolkit, and nothing Qt would
take for one of its own options).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

from . import PROGRAM, VERSION

USAGE = f"""Usage: {PROGRAM} [OPTION...] [-e COMMAND [ARGUMENT...]]

Open a terminal. When the terminal is already running, the new tab (or
window) opens in it.

Options:
  --working-directory DIR   start in DIR
  --title TITLE             a fixed title for the tab
  --profile NAME            use the profile NAME instead of the default one
  --new-window              open a new window instead of a tab
  --separate                run as a process of its own
  -e COMMAND [ARGUMENT...]  run COMMAND instead of the shell; everything after -e
                            belongs to the command
  --help                    show this text
  --version                 show the version
"""

_PREFIX = "--boswas-request="
_PIECE = 6000
MAX_ENCODED = 48 * 1024
MAX_ARGUMENTS = 4096
MAX_ARGUMENT = 32768


class UsageError(Exception):
    """The command line is wrong; the text says how."""


@dataclass
class Request:
    """What one start of the program asks for."""

    directory: str = ""                 # --working-directory, absolute
    title: str = ""
    profile: str = ""
    command: list[str] = field(default_factory=list)
    new_window: bool = False
    launch_directory: str = ""          # where the program was started


@dataclass
class Invocation:
    request: Request
    separate: bool = False
    show: str = ""                      # "help" or "version": print and exit


def parse(arguments: list[str], cwd: str | None = None) -> Invocation:
    """Read the command line (without the program name). Raises UsageError."""
    cwd = cwd if cwd is not None else _cwd()
    # Started from the desktop the directory is / (or unknown): that is no place the user chose.
    request = Request(launch_directory=cwd if cwd.startswith("/") and cwd != "/" else "")
    invocation = Invocation(request)
    index = 0

    def value(option: str, inline: str | None) -> str:
        nonlocal index
        if inline is not None:
            return inline
        index += 1
        if index >= len(arguments):
            raise UsageError(f"{option} needs a value")
        return arguments[index]

    while index < len(arguments):
        argument = arguments[index]
        name, separator, inline = argument.partition("=")
        inline_value = inline if separator and name.startswith("--") else None
        option = name if inline_value is not None else argument
        if option in ("-e", "-x", "--"):
            request.command = list(arguments[index + 1:])
            if option != "--" and not request.command:
                raise UsageError(f"{option} needs a command")
            if len(request.command) == 1 and " " in request.command[0] and not os.path.exists(request.command[0]):
                request.command = _split(request.command[0])
            break
        if option == "--working-directory":
            directory = value(option, inline_value)
            if not directory:
                raise UsageError("--working-directory needs a directory")
            request.directory = os.path.normpath(os.path.join(cwd or "/", os.path.expanduser(directory)))
        elif option in ("--title", "-T"):
            request.title = value(option, inline_value)
        elif option in ("--profile", "-p"):
            request.profile = value(option, inline_value)
        elif option == "--new-window" and inline_value is None:
            request.new_window = True
        elif option == "--separate" and inline_value is None:
            invocation.separate = True
        elif option in ("--help", "-h") and inline_value is None:
            invocation.show = "help"
        elif option == "--version" and inline_value is None:
            invocation.show = "version"
        elif argument.startswith(_PREFIX):
            raise UsageError("--boswas-request is for the program's own use")
        else:
            raise UsageError(f"unknown option {argument!r}" if argument.startswith("-")
                             else f"unexpected argument {argument!r} (a command goes after -e)")
        index += 1
    _check(request)
    return invocation


def _split(command: str) -> list[str]:
    import shlex
    try:
        return shlex.split(command)
    except ValueError as error:
        raise UsageError(f"the command is not written correctly: {error}") from None


def _cwd() -> str:
    try:
        return os.getcwd()
    except OSError:
        return ""


def _check(request: Request) -> None:
    if len(request.command) > MAX_ARGUMENTS or any(len(a) > MAX_ARGUMENT or "\0" in a for a in request.command):
        raise UsageError("the command is too long")
    for text in (request.directory, request.title, request.profile):
        if "\0" in text or len(text) > 4096:
            raise UsageError("an option value is too long")


def version_text() -> str:
    return f"{PROGRAM} {VERSION}"


def encode(request: Request) -> list[str] | None:
    """The request as arguments for the running instance; None when it is too large to pass on."""
    text = json.dumps(asdict(request), ensure_ascii=True, separators=(",", ":"))
    if len(text) > MAX_ENCODED:
        return None
    return [_PREFIX + text[start:start + _PIECE] for start in range(0, len(text), _PIECE)] or [_PREFIX]


def decode(arguments: list[str]) -> Request:
    """A request from the arguments another start sent. Anything that is not a valid request is an empty one."""
    text = "".join(argument[len(_PREFIX):] for argument in arguments
                   if isinstance(argument, str) and argument.startswith(_PREFIX))
    if not text or len(text) > MAX_ENCODED:
        return Request()
    try:
        data = json.loads(text)
    except ValueError:
        return Request()
    if not isinstance(data, dict):
        return Request()

    def string(key: str, limit: int = 4096) -> str:
        item = data.get(key)
        return item if isinstance(item, str) and len(item) <= limit and "\0" not in item else ""

    command = data.get("command")
    if (not isinstance(command, list) or len(command) > MAX_ARGUMENTS
            or not all(isinstance(item, str) and "\0" not in item and len(item) <= MAX_ARGUMENT for item in command)):
        command = []
    directory = string("directory")
    launch_directory = string("launch_directory")
    return Request(directory=directory if directory.startswith("/") else "", title=string("title", 1024),
                   profile=string("profile", 256), command=list(command), new_window=data.get("new_window") is True,
                   launch_directory=launch_directory if launch_directory.startswith("/") else "")
