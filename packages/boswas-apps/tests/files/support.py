"""Shared pieces of the Files tests: a temporary home, small file helpers."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for extra in (str(ROOT), str(ROOT.parents[1] / "desktop" / "ui" / "qt")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

_ENVIRONMENT = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_DATA_DIRS",
                "XDG_CONFIG_DIRS", "XDG_CURRENT_DESKTOP", "LANG", "LC_ALL", "LC_MESSAGES")


class TempCase(unittest.TestCase):
    """A test with ``self.tmp`` (a private directory) as HOME and as every XDG base directory."""

    def setUp(self) -> None:
        super().setUp()
        self._saved = {name: os.environ.get(name) for name in _ENVIRONMENT}
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="files-test-"))
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        os.environ["HOME"] = self.home
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.home, ".config")
        os.environ["XDG_DATA_HOME"] = os.path.join(self.home, ".local", "share")
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.home, ".cache")
        os.environ["XDG_STATE_HOME"] = os.path.join(self.home, ".local", "state")
        os.environ["XDG_DATA_DIRS"] = os.path.join(self.tmp, "share")
        os.environ["XDG_CONFIG_DIRS"] = os.path.join(self.tmp, "xdg")
        for name in ("XDG_CURRENT_DESKTOP", "LC_ALL", "LC_MESSAGES"):
            os.environ.pop(name, None)
        os.environ["LANG"] = "C.UTF-8"

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        make_removable(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def path(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)

    def write(self, relative: str, content: bytes | str = b"x", mode: int | None = None) -> str:
        target = self.path(relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(content.encode() if isinstance(content, str) else content)
        if mode is not None:
            os.chmod(target, mode)
        return target

    def mkdir(self, relative: str) -> str:
        target = self.path(relative)
        os.makedirs(target, exist_ok=True)
        return target

    def read(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()


def make_removable(root: str) -> None:
    for directory, names, _files in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                try:
                    os.chmod(path, 0o700)
                except OSError:
                    pass


def tree(root: str) -> dict[str, object]:
    """A folder as a dict: file -> bytes, link -> ("link", target), folder -> dict."""
    result: dict[str, object] = {}
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if os.path.islink(path):
            result[name] = ("link", os.readlink(path))
        elif os.path.isdir(path):
            result[name] = tree(path)
        else:
            with open(path, "rb") as handle:
                result[name] = handle.read()
    return result


IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
