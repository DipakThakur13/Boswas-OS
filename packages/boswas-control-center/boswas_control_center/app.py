"""boswas-control-center: entry point of Boswas Control Center.

  boswas-control-center                    open the window
  boswas-control-center --page PAGE        open the window at PAGE, a category
                                           (system, network, ...) or a page
                                           (display, wifi, storage, ...);
                                           the keys of earlier versions lead to
                                           where their settings are now
  boswas-control-center --page PAGE --section SECTION
                                           and scroll to that settings section
                                           of the page (wi-fi, night-light, ...)
  boswas-control-center --version

Only one Control Center runs per session (boswas_fluent.run): a second start
hands its arguments to the window that is already open, which comes to the
front and shows the page asked for, and then exits with 0.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import textwrap

from . import __version__
from .catalog import PAGE_KEYS

DESKTOP_ID = "com.boswas.ControlCenter"
DISPLAY_NAME = "Boswas Control Center"
EXIT_ROOT = 4
SECTION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def section_name(value: str) -> str:
    """A --section value: a section's title in lower case with dashes (argparse type)."""
    name = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not SECTION_RE.fullmatch(name):
        raise argparse.ArgumentTypeError("a section is named by its title, for example wi-fi or night-light")
    return name


class _Help(argparse.HelpFormatter):
    """Help text that never breaks a page name at its hyphen."""

    def _split_lines(self, text, width):
        return textwrap.wrap(text, width, break_on_hyphens=False, break_long_words=False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="boswas-control-center", formatter_class=_Help,
                                     description="Boswas Control Center: the settings hub of Boswas OS.")
    parser.add_argument("--page", choices=PAGE_KEYS, metavar="PAGE",
                        help="open this category or page: " + ", ".join(PAGE_KEYS))
    parser.add_argument("--section", type=section_name, metavar="SECTION",
                        help="with --page: scroll to this settings section of the page, named by its title in "
                             "lower case with dashes (wi-fi, night-light, date-time)")
    parser.add_argument("--version", action="version", version=f"boswas-control-center {__version__}")
    return parser


def requested_page(arguments: list[str]) -> tuple[str | None, str | None]:
    """(page, section) named by command-line arguments; never raises and never exits.

    Used for the arguments another start of the program handed over: what
    is not a known page or a well-formed section name is ignored.
    """
    page = section = None
    values = [a for a in arguments if isinstance(a, str)]
    for index, argument in enumerate(values):
        for option in ("--page", "--section"):
            if argument == option:
                value = values[index + 1] if index + 1 < len(values) else ""
            elif argument.startswith(option + "="):
                value = argument[len(option) + 1:]
            else:
                continue
            if option == "--page" and value in PAGE_KEYS:
                page = value
            elif option == "--section":
                try:
                    section = section_name(value)
                except argparse.ArgumentTypeError:
                    section = None
    return page, (section if page is not None else None)


def running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    # --help and --version must work without a display, and a wrong page must be reported by the start
    # that named it, so parse before Qt starts; Qt's own options (-platform, -style, ...) are left to Qt.
    _parser().parse_known_args(argv[1:])
    if running_as_root():
        sys.stderr.write("boswas-control-center: runs as the desktop user, never as root\n")
        return EXIT_ROOT

    import boswas_fluent as fluent

    from .backend import Backend
    from .ui.main_window import MainWindow

    windows: list = []

    def open_window(arguments: list[str]):
        page, section = requested_page(arguments)
        window = MainWindow(Backend(), initial_page=page, initial_section=section)
        windows.append(window)
        return window

    try:
        code = fluent.run(DESKTOP_ID, open_window, argv=argv, display_name=DISPLAY_NAME, version=__version__,
                          icon="boswas-control-center")
    finally:
        for window in windows:
            window.shutdown()
    if any(window.runner.running() for window in windows):
        # A read (the security checks, the session service) is still running
        # and QThreadPool would wait for it at exit; it has no side effects.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    return code
