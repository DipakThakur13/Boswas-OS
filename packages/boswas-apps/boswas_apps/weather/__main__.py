"""boswas-weather: the forecast for the places you choose. See boswas-weather(1)."""

from __future__ import annotations

import sys

from . import APP_ID

USAGE = """Usage: boswas-weather [--place NAME]

Weather for the Boswas desktop: the forecast of the places you add, from Open-Meteo.

  --place NAME  open with the forecast of one of your places
  --version     print the version and exit
  --help        print this text and exit
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--help" in argv[1:] or "-h" in argv[1:]:
        sys.stdout.write(USAGE)
        return 0
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
