"""boswas-calendar: a local calendar with iCalendar import and export. See boswas-calendar(1)."""

from __future__ import annotations

import sys

from . import APP_ID

USAGE = """Usage: boswas-calendar [--new] [--show UID [--occurrence START]] [FILE.ics ...]

Calendar for the Boswas desktop.

  FILE.ics            show what the file holds and ask which calendar to import it into
  --new               open the dialog for a new event
  --show UID          go to the event with this UID and open it
  --occurrence START  with --show: the occurrence of a repeating event that starts then
                      (as in the series: 2026-11-17T09:00:00, or 2026-11-17 for an all-day event)
  --version           print the version and exit
  --help              print this text and exit
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--help" in argv[1:] or "-h" in argv[1:]:
        sys.stdout.write(USAGE)
        return 0
    if "--version" in argv[1:]:
        from .ui.window import VERSION
        sys.stdout.write(f"boswas-calendar {VERSION}\n")
        return 0
    import os

    import boswas_fluent as fluent

    from .ui.window import VERSION, CalendarWindow
    # A running Calendar gets the arguments passed on: its working directory is another one, so files
    # are named in full here.
    arguments = [argv[0]] + [os.path.abspath(argument) if not argument.startswith("-") else argument
                             for argument in argv[1:]]
    return fluent.run(APP_ID, CalendarWindow, argv=arguments, display_name="Calendar", version=VERSION,
                      icon="office-calendar")


if __name__ == "__main__":
    sys.exit(main())
