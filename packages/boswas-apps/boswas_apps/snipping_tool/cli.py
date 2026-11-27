"""The command line of boswas-snipping-tool (standard library only)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

USAGE = """Usage: boswas-snipping-tool [OPTION]... [FILE]

Snip the screen, mark the snip up, save it or copy it.

  FILE           a PNG, JPEG or WebP picture to mark up
  --new          start a snip straight away
  --mode=MODE    what a snip takes: region (an area you drag, the default)
                 or screen (the whole screen)
  --delay=N      wait N seconds (0 to 60) before the snip
  --new-window   accepted, and the same as a start without it: Snipping Tool
                 has one window, which comes forward
  --version      print the version and exit
  -h, --help     print this text and exit
"""
MODES = ("region", "screen")
MODE_ALIASES = {"region": "region", "rectangle": "region", "rect": "region", "area": "region",
                "screen": "screen", "full": "screen", "fullscreen": "screen", "full-screen": "screen"}
MAX_DELAY = 60
_ADDRESS = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


@dataclass
class Arguments:
    path: str | None = None          # the picture to open, absolute
    new: bool = False
    mode: str | None = None          # "region" or "screen"; None: as set
    delay: int | None = None         # seconds; None: as set
    help: bool = False
    version: bool = False
    errors: list[str] = field(default_factory=list)

    def forwarded(self) -> list[str]:
        """The arguments again, with an absolute file name: what a running instance is handed."""
        result = []
        if self.new:
            result.append("--new")
        if self.mode is not None:
            result.append(f"--mode={self.mode}")
        if self.delay is not None:
            result.append(f"--delay={self.delay}")
        if self.path is not None:
            result += ["--", self.path]
        return result


def local_path(argument: str, base: str | None = None) -> str | None:
    """A command line argument as an absolute path: a ``file:`` address or a file name. None for anything else."""
    if not argument or "\x00" in argument:
        return None
    if argument.startswith("file:"):
        parts = urlsplit(argument)
        if parts.netloc not in ("", "localhost"):
            return None
        argument = unquote(parts.path)
        if not argument.startswith("/") or "\x00" in argument:
            return None
    elif _ADDRESS.match(argument):
        return None                                   # some other address: not a local file
    if not os.path.isabs(argument):
        argument = os.path.join(base or os.getcwd(), argument)
    return os.path.normpath(argument)


def parse_mode(text: str) -> str | None:
    return MODE_ALIASES.get(text.strip().lower())


def parse_delay(text: str) -> int | None:
    text = text.strip()
    if not text.isascii() or not text.isdigit() or len(text) > 3:
        return None
    value = int(text)
    return value if 0 <= value <= MAX_DELAY else None


def parse(arguments: list[str], base: str | None = None) -> Arguments:
    """Read the arguments (without the program name). Mistakes are collected in ``errors``, never raised."""
    result = Arguments()
    only_paths = False
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if not only_paths and argument.startswith("-") and len(argument) > 1:
            name, has_value, value = argument.partition("=")
            if argument == "--":
                only_paths = True
            elif argument == "--new":
                result.new = True
            elif argument == "--new-window":
                pass                                  # one window: a later start brings it forward anyway
            elif argument in ("-h", "--help"):
                result.help = True
            elif argument == "--version":
                result.version = True
            elif name in ("--mode", "--delay"):
                if not has_value:
                    if index >= len(arguments):
                        result.errors.append(f"{name} needs a value")
                        continue
                    value = arguments[index]
                    index += 1
                if name == "--mode":
                    mode = parse_mode(value)
                    if mode is None:
                        result.errors.append(f"unknown mode {value!r} (region or screen)")
                    else:
                        result.mode = mode
                else:
                    delay = parse_delay(value)
                    if delay is None:
                        result.errors.append(f"the delay is a number of seconds from 0 to {MAX_DELAY}, not {value!r}")
                    else:
                        result.delay = delay
            else:
                result.errors.append(f"unknown option {argument}")
            continue
        path = local_path(argument, base)
        if path is None:
            result.errors.append(f"not a local file: {argument}")
        elif result.path is None:
            result.path = path
        else:
            result.errors.append("only one picture can be opened at a time")
    return result
