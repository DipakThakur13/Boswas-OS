"""boswas-calculator: start the calculator. See boswas-calculator(1)."""

from __future__ import annotations

import argparse
import sys

from . import APP_ID, DISPLAY_NAME, state


def parse_arguments(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="boswas-calculator",
        description="Calculator with Standard, Scientific and Programmer modes, date calculation and unit "
                    "converters.")
    parser.add_argument("--mode", choices=state.PAGES, metavar="MODE",
                        help="open in this mode instead of the last one: " + ", ".join(state.PAGES))
    parser.add_argument("--new-window", action="store_true",
                        help="open a new calculator (every start is a calculator of its own anyway)")
    parser.add_argument("--version", action="version", version=f"boswas-calculator {_version()}")
    return parser.parse_known_args(argv)


def _version() -> str:
    from .. import __version__
    return __version__


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    options, rest = parse_arguments(argv[1:])
    import boswas_fluent as fluent

    from .ui.window import CalculatorWindow

    def window(_arguments: list[str]) -> CalculatorWindow:
        return CalculatorWindow([options.mode] if options.mode else [])

    # Every start is its own calculator, as in Windows: there is no single instance to hand over to.
    return fluent.run(APP_ID, window, argv=[argv[0] if argv else "boswas-calculator", *rest],
                      display_name=DISPLAY_NAME, version=_version(), icon="accessories-calculator",
                      single_instance=False)


if __name__ == "__main__":
    sys.exit(main())
