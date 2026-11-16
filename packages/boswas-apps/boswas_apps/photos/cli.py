"""The command line of boswas-photos (standard library only)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

USAGE = """Usage: boswas-photos [OPTION]... [FILE|FOLDER]...

Show pictures. Without a file the gallery opens.

  FILE              a picture: it is shown, with the other pictures of its
                    folder one arrow key away
  FOLDER            the pictures of that folder
  several files     these pictures, in the order given

Options:
  --new-window      open in a new window even when Photos is running
  --slideshow       start a slideshow of the pictures given
  --gallery         show the gallery
  -h, --help        show this text and exit
  --version         show the version and exit
"""
MAX_PATHS = 2000
_ADDRESS = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


@dataclass
class Arguments:
    paths: list[str] = field(default_factory=list)
    new_window: bool = False
    slideshow: bool = False
    gallery: bool = False
    help: bool = False
    version: bool = False
    unknown: list[str] = field(default_factory=list)


def local_path(argument: str, base: str | None = None) -> str | None:
    """A command line argument as an absolute path: a ``file:`` address or a file name. None for anything else."""
    if not argument or "\x00" in argument:
        return None
    if argument.startswith("file:"):
        parts = urlsplit(argument)
        if parts.netloc not in ("", "localhost"):
            return None
        argument = unquote(parts.path)
        if not argument.startswith("/"):
            return None
    elif _ADDRESS.match(argument):
        return None                                   # some other address: not a local file
    if not os.path.isabs(argument):
        argument = os.path.join(base or os.getcwd(), argument)
    return os.path.normpath(argument)


def parse(arguments: list[str], base: str | None = None) -> Arguments:
    """Read the arguments (without the program name). Unknown options are collected, not fatal."""
    result = Arguments()
    only_paths = False
    for argument in arguments:
        if not only_paths and argument.startswith("-") and len(argument) > 1:
            if argument == "--":
                only_paths = True
            elif argument == "--new-window":
                result.new_window = True
            elif argument == "--slideshow":
                result.slideshow = True
            elif argument == "--gallery":
                result.gallery = True
            elif argument in ("-h", "--help"):
                result.help = True
            elif argument == "--version":
                result.version = True
            else:
                result.unknown.append(argument)
            continue
        path = local_path(argument, base)
        if path is not None and len(result.paths) < MAX_PATHS:
            result.paths.append(path)
    return result


def absolute(arguments: list[str], base: str | None = None) -> list[str]:
    """The arguments with every file name made absolute, to hand to an instance that runs elsewhere."""
    result = []
    only_paths = False
    for argument in arguments:
        if not only_paths and argument.startswith("-") and len(argument) > 1:
            if argument == "--":
                only_paths = True
            result.append(argument)
            continue
        path = local_path(argument, base)
        if path is not None:
            result.append(path)
    return result
