"""Argument validation. Nothing reaches a command line or a path before passing here."""

from __future__ import annotations

import os
import re

from .protocol import SysopsError

PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,99}(:(amd64|i386|all))?$")
MAX_PACKAGES = 50
REMOTE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
FIRMWARE_FILE = re.compile(r"^[A-Za-z0-9_+][A-Za-z0-9._+,@-]*(/[A-Za-z0-9_+][A-Za-z0-9._+,@-]*){0,7}$")
MAX_LOCAL_DEB = 2 * 1024 * 1024 * 1024
SERVICE_VERBS = ("start", "stop", "restart")


def no_arguments(args: list[str]) -> list[str]:
    if args:
        raise SysopsError("INVALID_ARGUMENT", "This operation takes no arguments.")
    return []


def packages(args: list[str]) -> list[str]:
    if not 1 <= len(args) <= MAX_PACKAGES:
        raise SysopsError("INVALID_ARGUMENT", f"Name between 1 and {MAX_PACKAGES} packages.")
    seen = []
    for name in args:
        if not isinstance(name, str) or not PACKAGE.fullmatch(name):
            raise SysopsError("INVALID_ARGUMENT", f"{name!r} is not a valid package name.")
        if name not in seen:
            seen.append(name)
    return seen


def bare_name(package: str) -> str:
    """The package name without an architecture qualifier."""
    return package.split(":", 1)[0]


def local_deb_path(args: list[str]) -> str:
    if len(args) != 1:
        raise SysopsError("INVALID_ARGUMENT", "Name exactly one .deb file.")
    path = args[0]
    if (not isinstance(path, str) or not path.startswith("/") or "\0" in path or "\n" in path or len(path) > 4096
            or os.path.normpath(path) != path or "/../" in f"{path}/" or not path.endswith(".deb")
            or path.endswith("/.deb")):
        raise SysopsError("INVALID_ARGUMENT", "The package file must be given as an absolute path to a .deb file.")
    return path


def remote_name(args: list[str]) -> str:
    if len(args) != 1 or not isinstance(args[0], str) or not REMOTE_NAME.fullmatch(args[0]):
        raise SysopsError("INVALID_ARGUMENT", "Name exactly one Flatpak remote (lower-case letters, digits, . _ -).")
    return args[0]


def remove_options(args: list[str]) -> list[str]:
    if args not in ([], ["--purge-data"]):
        raise SysopsError("INVALID_ARGUMENT", "The only option is --purge-data.")
    return list(args)


def service_verb(args: list[str]) -> str:
    if len(args) != 1 or args[0] not in SERVICE_VERBS:
        raise SysopsError("INVALID_ARGUMENT", "Use start, stop or restart.")
    return args[0]


def firmware_file(name: str) -> bool:
    return bool(FIRMWARE_FILE.fullmatch(name)) and ".." not in name.split("/") and len(name) <= 200
