"""boswas-snipping-tool: snip the screen, mark the snip up, save or copy it. See boswas-snipping-tool(1)."""

from __future__ import annotations

import sys

from . import APP_ID, DISPLAY_NAME, PROGRAM, cli

USAGE = cli.USAGE


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    arguments = cli.parse(argv[1:])
    if arguments.help:
        sys.stdout.write(USAGE)
        return 0
    if arguments.version:
        from .ui.window import VERSION
        sys.stdout.write(f"{PROGRAM} {VERSION}\n")
        return 0
    if arguments.errors:
        sys.stderr.write(f"{PROGRAM}: {arguments.errors[0]}\n{USAGE}")
        return 2
    import boswas_fluent as fluent

    from .ui.window import VERSION, SnipWindow

    # A running Snipping Tool may have another working directory: hand it an absolute file name.
    forwarded = [argv[0] if argv else PROGRAM, *arguments.forwarded()]
    return fluent.run(APP_ID, SnipWindow, argv=forwarded, display_name=DISPLAY_NAME, version=VERSION,
                      icon="boswas-snipping-tool")


if __name__ == "__main__":
    sys.exit(main())
