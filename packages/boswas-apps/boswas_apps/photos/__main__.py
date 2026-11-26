"""boswas-photos: start the application. See boswas-photos(1)."""

from __future__ import annotations

import sys

from . import APP_ID, DISPLAY_NAME, PROGRAM, cli
from .. import __version__


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    arguments = cli.parse(argv[1:])
    if arguments.help:
        sys.stdout.write(cli.USAGE)
        return 0
    if arguments.version:
        sys.stdout.write(f"{PROGRAM} {__version__}\n")
        return 0
    if arguments.unknown:
        sys.stderr.write(f"{PROGRAM}: unknown option {arguments.unknown[0]}\n{cli.USAGE}")
        return 2
    import boswas_fluent as fluent

    from .ui.window import PhotosWindow

    # A running instance may have another working directory: hand it absolute names.
    forwarded = [argv[0] if argv else PROGRAM, *cli.absolute(argv[1:])]
    # --new-window of a later start: fluent.run builds a window of its own in the running Photos.
    return fluent.run(APP_ID, PhotosWindow, argv=forwarded, display_name=DISPLAY_NAME, version=__version__,
                      icon="boswas-photos", multiple_windows=True)


if __name__ == "__main__":
    sys.exit(main())
