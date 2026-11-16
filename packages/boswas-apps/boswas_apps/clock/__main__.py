"""boswas-clock: alarms, timers, a stopwatch, a world clock and focus sessions. See boswas-clock(1)."""

from __future__ import annotations

import sys

from . import APP_ID

USAGE = """Usage: boswas-clock [--page PAGE]

Clock for the Boswas desktop.

  --page PAGE   open with a page: focus, timers, alarms, stopwatch, world or settings
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
        sys.stdout.write(f"boswas-clock {VERSION}\n")
        return 0
    import boswas_fluent as fluent

    from .ui.window import VERSION, ClockWindow
    return fluent.run(APP_ID, ClockWindow, argv=argv, display_name="Clock", version=VERSION,
                      icon="preferences-system-time")


if __name__ == "__main__":
    sys.exit(main())
