"""boswas-document-viewer: the command line of Document Viewer. See boswas-document-viewer(1)."""

from __future__ import annotations

import argparse
import os
import sys

from . import APP_ID, DISPLAY_NAME, PROGRAM
from .. import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROGRAM, description="Read PDF documents.",
                                     usage=f"{PROGRAM} [FILE ...]")
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {__version__}")
    parser.add_argument("files", nargs="*", metavar="FILE", help="PDF documents to open, each in a tab")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    # --help and --version work without a display: the command line is read before Qt starts.
    options, qt_arguments = build_parser().parse_known_args(argv[1:])
    # Whole paths: a later start hands its files to the window that is already open, whose working
    # directory is another one.
    files = [os.path.abspath(path) for path in options.files]

    import boswas_fluent as fluent

    def factory(arguments: list[str]):
        from .window import ViewerWindow
        return ViewerWindow(arguments)

    return fluent.run(APP_ID, factory, argv=[argv[0], *qt_arguments, *files], display_name=DISPLAY_NAME,
                      version=__version__, icon="boswas-document-viewer")


if __name__ == "__main__":
    sys.exit(main())
