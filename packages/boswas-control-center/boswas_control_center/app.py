"""boswas-control-center: entry point of Boswas Control Center.

  boswas-control-center               open the window
  boswas-control-center --page PAGE   open the window at PAGE (security,
                                      applications, updates, ...)
  boswas-control-center --version
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .catalog import PAGE_KEYS

DESKTOP_ID = "com.boswas.ControlCenter"
EXIT_ROOT = 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="boswas-control-center",
                                     description="Boswas Control Center: the settings hub of Boswas OS.")
    parser.add_argument("--page", choices=PAGE_KEYS, metavar="PAGE",
                        help="open this page: " + ", ".join(PAGE_KEYS))
    parser.add_argument("--version", action="version", version=f"boswas-control-center {__version__}")
    return parser


def running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    # --help and --version must work without a display, so parse before Qt
    # starts; Qt's own options (-platform, -style, ...) are left to Qt.
    args, _qt_args = _parser().parse_known_args(argv[1:])
    if running_as_root():
        sys.stderr.write("boswas-control-center: runs as the desktop user, never as root\n")
        return EXIT_ROOT

    from PySide6.QtWidgets import QApplication

    from .backend import Backend
    from .ui.common import app_icon
    from .ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName("boswas-control-center")
    app.setApplicationDisplayName("Boswas Control Center")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Boswas")
    app.setDesktopFileName(DESKTOP_ID)
    app.setWindowIcon(app_icon())

    window = MainWindow(Backend(), initial_page=args.page)
    window.show()
    try:
        code = app.exec()
    finally:
        window.shutdown()
    if window.runner.running():
        # A read (the security checks, the session service) is still running
        # and QThreadPool would wait for it at exit; it has no side effects.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    return code
