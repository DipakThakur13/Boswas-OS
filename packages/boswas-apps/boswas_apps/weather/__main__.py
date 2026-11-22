"""boswas-weather: the forecast for the places you choose. See boswas-weather(1)."""

from __future__ import annotations

import os
import sys

from . import APP_ID

USAGE = """Usage: boswas-weather [--place NAME]
       boswas-weather --update-lock-screen

Weather for the Boswas desktop: the forecast of the places you add, from Open-Meteo.

  --place NAME          open with the forecast of one of your places
  --update-lock-screen  fetch the forecast of your home place for the lock screen and exit (no window;
                        does nothing without a place or when the lock screen does not show the weather)
  --version             print the version and exit
  --help                print this text and exit
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--help" in argv[1:] or "-h" in argv[1:]:
        sys.stdout.write(USAGE)
        return 0
    if "--update-lock-screen" in argv[1:]:
        if os.geteuid() == 0:
            # It would leave files and directories of root in a user's home.
            sys.stderr.write("boswas-weather: runs as the desktop user, never as root\n")
            return 4
        from .lockscreen import update        # no Qt: this runs from a systemd user timer
        return update()
    if "--version" in argv[1:]:
        from .ui.window import VERSION
        sys.stdout.write(f"boswas-weather {VERSION}\n")
        return 0
    import boswas_fluent as fluent

    from .ui.window import VERSION, WeatherWindow
    return fluent.run(APP_ID, WeatherWindow, argv=argv, display_name="Weather", version=VERSION,
                      icon="boswas-weather")


if __name__ == "__main__":
    sys.exit(main())
