"""boswas-camera: start Camera, or bring the one that is running to the front."""

from __future__ import annotations

import argparse
import sys

from . import APP_ID, __version__


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="boswas-camera", allow_abbrev=False,
        description="Take photos and record videos with a camera of this computer.")
    parser.add_argument("--version", action="version", version=f"boswas-camera {__version__}")
    _options, qt_options = parser.parse_known_args(argv[1:])

    import boswas_fluent as fluent

    def factory(arguments: list[str]):
        from .window import CameraWindow
        return CameraWindow(arguments)

    return fluent.run(APP_ID, factory, argv=[argv[0] if argv else "boswas-camera", *qt_options],
                      display_name="Camera", version=__version__, icon="camera-photo")


if __name__ == "__main__":
    sys.exit(main())
