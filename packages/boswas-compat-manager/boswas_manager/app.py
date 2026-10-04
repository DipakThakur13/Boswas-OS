"""boswas-compat-manager: entry point of the Compatibility Manager.

  boswas-compat-manager                 open the window
  boswas-compat-manager --install FILE  open the install flow and check FILE
                                        (the Dolphin "Install with Boswas
                                        Compatibility Manager" action)
  boswas-compat-manager --version
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__

DESKTOP_ID = "com.boswas.CompatibilityManager"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="boswas-compat-manager",
                                     description="Install, run and manage Windows applications on Boswas OS.")
    parser.add_argument("--install", metavar="FILE", help="check FILE (.exe or .msi) and offer to install it")
    parser.add_argument("--version", action="version", version=f"boswas-compat-manager {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    # --help and --version must work without a display, so parse before Qt
    # starts; Qt's own options (-platform, -style, ...) are left to Qt.
    args, _qt_args = _parser().parse_known_args(argv[1:])
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        sys.stderr.write("boswas-compat-manager: runs as the desktop user, never as root\n")
        return 4

    from PySide6.QtWidgets import QApplication

    from .backend import Backend
    from .ui.common import app_icon
    from .ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName("boswas-compat-manager")
    app.setApplicationDisplayName("Boswas Compatibility Manager")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Boswas")
    app.setDesktopFileName(DESKTOP_ID)
    app.setWindowIcon(app_icon())

    window = MainWindow(Backend())
    window.show()
    if args.install:
        # The session agent requires an absolute path; it checks the file itself.
        window.open_install(os.path.abspath(args.install))
    try:
        code = app.exec()
    finally:
        window.shutdown()
    if window.runner.running():
        # A call is still waiting for the session agent (checking a large
        # installer can take minutes) and QThreadPool would wait for it at
        # exit. The service completes the operation on its own; do not keep
        # an invisible process around for the answer.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    return code
