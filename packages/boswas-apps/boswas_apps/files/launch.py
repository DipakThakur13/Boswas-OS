"""Starting other programs: the application of a file, a terminal, another Files window.

Programs are started detached with an argument list (QProcess); nothing
goes through a shell. ``Launcher.start`` is the single place that starts a
process, so tests replace it.
"""

from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import QProcess

from . import desktopapps
from .desktopapps import DesktopApp, ExecError

TERMINALS = (("boswas-terminal", "--working-directory"), ("konsole", "--workdir"))
TERMINAL_RUN = (("boswas-terminal", "-e"), ("konsole", "-e"))


class LaunchError(Exception):
    """A program could not be started; the text is for the user."""


def _start_detached(program: str, arguments: list[str], directory: str) -> bool:
    process = QProcess()
    process.setProgram(program)
    process.setArguments(arguments)
    if directory and os.path.isdir(directory):
        process.setWorkingDirectory(directory)
    started = process.startDetached()
    return bool(started[0] if isinstance(started, tuple) else started)


class Launcher:
    def __init__(self) -> None:
        self.start: Callable[[str, list[str], str], bool] = _start_detached
        self.find = desktopapps.find_program

    def _run(self, argv: list[str], directory: str) -> None:
        program = self.find(argv[0])
        if program is None:
            raise LaunchError(f"The program “{argv[0]}” was not found.")
        if not self.start(program, list(argv[1:]), directory):
            raise LaunchError(f"“{os.path.basename(program)}” could not be started.")

    def terminal_prefix(self) -> list[str]:
        for name, option in TERMINAL_RUN:
            if self.find(name):
                return [name, option]
        raise LaunchError("No terminal application was found (boswas-terminal or konsole).")

    def open_with(self, app: DesktopApp, paths: list[str]) -> None:
        """Open ``paths`` with ``app``. Raises LaunchError."""
        try:
            commands = desktopapps.expand_exec(app, paths)
        except ExecError as error:
            raise LaunchError(f"“{app.name}” cannot be started: {error}.") from None
        directory = app.working_dir or (os.path.dirname(paths[0]) if paths else "")
        prefix = self.terminal_prefix() if app.terminal else []
        for argv in commands:
            self._run(prefix + argv, directory)

    def run_program(self, path: str, in_terminal: bool = False) -> None:
        """Run an executable file the user chose to run."""
        directory = os.path.dirname(path)
        if in_terminal:
            self._run(self.terminal_prefix() + [path], directory)
        else:
            self._run([path], directory)

    def open_terminal(self, directory: str) -> None:
        """A terminal in ``directory``: boswas-terminal, else konsole. Raises LaunchError."""
        for name, option in TERMINALS:
            if self.find(name):
                self._run([name, option, directory], directory)
                return
        raise LaunchError("No terminal application was found (boswas-terminal or konsole).")

    def new_window(self, locations: list[str]) -> None:
        self._run(["boswas-files", "--new-window", *locations], "")
