"""boswas-media-player: start Media Player, or hand files to the one that is running."""

from __future__ import annotations

import argparse
import os
import sys

from . import APP_ID, __version__


def _arguments(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="boswas-media-player", allow_abbrev=False,
        description="Play music and videos. Files, folders, playlists (M3U, M3U8, PLS) and http(s) addresses given "
                    "here are played; when Media Player is already running they are added to its play queue.")
    parser.add_argument("--version", action="version", version=f"boswas-media-player {__version__}")
    parser.add_argument("--backend-info", action="store_true",
                        help="print the formats the multimedia backend of this system reports, and exit")
    parser.add_argument("items", nargs="*", metavar="FILE|FOLDER|URL", help="what to play")
    return parser.parse_known_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    options, qt_options = _arguments(argv[1:])
    if options.backend_info:
        from PySide6.QtCore import QCoreApplication
        from .backend import describe_backend
        _app = QCoreApplication(argv[:1])
        for name, values in describe_backend().items():
            print(f"{name}: {values if isinstance(values, str) else ', '.join(values)}")
        return 0

    import boswas_fluent as fluent
    from . import opening

    # Relative paths are resolved here: the instance that is already running has another working directory.
    cwd = os.getcwd()
    items = [location for location in (opening.to_location(item, cwd) for item in options.items) if location]

    def factory(arguments: list[str]):
        from .window import MainWindow
        return MainWindow(arguments)

    return fluent.run(APP_ID, factory, argv=[argv[0] if argv else "boswas-media-player", *qt_options, *items],
                      display_name="Media Player", version=__version__, icon="multimedia-player")


if __name__ == "__main__":
    sys.exit(main())
