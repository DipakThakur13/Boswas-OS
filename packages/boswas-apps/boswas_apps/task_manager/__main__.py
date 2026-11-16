"""boswas-task-manager: the program. See boswas-task-manager(1)."""

from __future__ import annotations

import sys

from . import APP_ID, DISPLAY_NAME, PAGES, PROGRAM

USAGE = f"""Usage: {PROGRAM} [--page PAGE]

Task Manager shows what is running on this computer and lets you end it.

Options:
  --page PAGE   open with this page: {", ".join(PAGES)}
  --version     print the version and exit
  -h, --help    print this text and exit

If Task Manager is already running, its window comes to the front (and
shows PAGE, if one is given).
"""


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "-h" in arguments or "--help" in arguments:
        sys.stdout.write(USAGE)
        return 0
    if "--version" in arguments:
        from .. import __version__
        sys.stdout.write(f"{PROGRAM} {__version__}\n")
        return 0
    for index, argument in enumerate(arguments):
        value = None
        if argument == "--page":
            value = arguments[index + 1] if index + 1 < len(arguments) else ""
        elif argument.startswith("--page="):
            value = argument[7:]
        if value is not None and value not in PAGES:
            sys.stderr.write(f"{PROGRAM}: there is no page “{value}”. Pages: {', '.join(PAGES)}\n")
            return 2

    import boswas_fluent as fluent

    from .. import __version__
    from .ui.window import TaskManagerWindow

    return fluent.run(APP_ID, TaskManagerWindow, argv=[PROGRAM, *arguments], display_name=DISPLAY_NAME,
                      version=__version__, icon="utilities-system-monitor")


if __name__ == "__main__":
    sys.exit(main())
