"""boswas-archive-manager: the command line of Archive Manager. See boswas-archive-manager(1)."""

from __future__ import annotations

import argparse
import os
import sys

from . import APP_ID, DISPLAY_NAME, PROGRAM
from .. import __version__



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM, description="Open, extract and create ZIP and TAR archives.",
        usage=f"{PROGRAM} [ARCHIVE ...]\n"
              f"       {PROGRAM} --extract ARCHIVE ... [--to FOLDER]\n"
              f"       {PROGRAM} --extract-here ARCHIVE ...\n"
              f"       {PROGRAM} --create OUTPUT FILE ...")
    parser.add_argument("--extract", action="store_true",
                        help="extract each ARCHIVE into a folder named after it, beside it (or into --to FOLDER), "
                             "showing only a progress window")
    parser.add_argument("--extract-here", action="store_true",
                        help="extract each ARCHIVE into the folder it is in, showing only a progress window")
    parser.add_argument("--to", metavar="FOLDER", help="with --extract: the folder to extract into")
    parser.add_argument("--create", action="store_true",
                        help="pack the FILEs into the new archive OUTPUT (.zip, .tar, .tar.gz, .tar.xz, .tar.bz2), "
                             "showing only a progress window")
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {__version__}")
    parser.add_argument("paths", nargs="*", metavar="PATH", help="archives to open or extract, or OUTPUT and FILEs")
    return parser


class Plan:
    """What one start of the program does."""

    def __init__(self, mode: str, archives: list[str], tasks: list[tuple]) -> None:
        self.mode = mode                # "browse" or "progress"
        self.archives = archives        # for "browse"
        self.tasks = tasks              # for "progress": (kind, path, target, sources)


def plan(arguments: list[str], parser: argparse.ArgumentParser | None = None) -> tuple[Plan, list[str]]:
    """The Plan for a command line, and the arguments that are left for Qt. Exits with a usage error."""
    parser = parser or build_parser()
    options, rest = parser.parse_known_args(arguments)
    unknown = [argument for argument in rest if argument.startswith("--")]
    if unknown:
        parser.error(f"unknown option {unknown[0]}")
    modes = [name for name in ("extract", "extract_here", "create") if getattr(options, name)]
    if len(modes) > 1:
        parser.error("--extract, --extract-here and --create cannot be combined")
    if options.to and modes != ["extract"]:
        parser.error("--to belongs to --extract")
    paths = [os.path.abspath(path) for path in options.paths]
    if not modes:
        return Plan("browse", paths, []), rest
    from .extract import default_destination
    if modes[0] == "create":
        if len(paths) < 2:
            parser.error("--create needs the archive to write and at least one file or folder")
        from .formats import format_for_new
        if format_for_new(paths[0]) is None:
            parser.error("the archive name must end in .zip, .tar, .tar.gz, .tar.xz or .tar.bz2")
        return Plan("progress", [], [("create", paths[0], None, paths[1:])]), rest
    if not paths:
        parser.error(f"--{modes[0].replace('_', '-')} needs at least one archive")
    tasks = []
    for path in paths:
        if modes[0] == "extract_here":
            target = os.path.dirname(path)
        elif options.to:
            target = os.path.abspath(options.to)
        else:
            target = default_destination(path)
        tasks.append(("extract", path, target, None))
    return Plan("progress", [], tasks), rest


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    # --help, --version and usage errors need no display: the command line is read before Qt starts.
    chosen, qt_arguments = plan(argv[1:])

    import boswas_fluent as fluent

    made: dict = {}

    def factory(_arguments: list[str]):
        if chosen.mode == "progress":
            from .ui.progress import ProgressWindow, Task
            made["window"] = ProgressWindow([Task(*task) for task in chosen.tasks])
        else:
            from .ui.window import ArchiveWindow
            first = ArchiveWindow(chosen.archives[0] if chosen.archives else None)
            for path in chosen.archives[1:]:               # one window for every archive that was named
                other = ArchiveWindow(path)
                first._others.append(other)
                other.show()
            made["window"] = first
        return made["window"]

    # Every start is its own process with its own window, as with other archive tools: an extraction
    # started from the Files application must not disturb an archive that is open in another window.
    code = fluent.run(APP_ID, factory, argv=[argv[0], *qt_arguments], display_name=DISPLAY_NAME,
                      version=__version__, icon="utilities-file-archiver", single_instance=False)
    window = made.get("window")
    return code or int(getattr(window, "exit_code", 0))


if __name__ == "__main__":
    sys.exit(main())
