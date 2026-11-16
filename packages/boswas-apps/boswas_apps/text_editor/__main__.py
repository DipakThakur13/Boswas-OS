"""boswas-text-editor: start the editor, or hand the files to the one that runs. See boswas-text-editor(1)."""

from __future__ import annotations

import argparse
import os
import sys

from . import APP_ID, DISPLAY_NAME

EXIT_ROOT = 4


def parse_arguments(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    from .. import __version__
    parser = argparse.ArgumentParser(prog="boswas-text-editor", description="Plain text editor with tabs.")
    parser.add_argument("files", nargs="*", metavar="FILE", help="text files to open, each in a tab")
    parser.add_argument("--new-window", action="store_true", help="open a new window")
    parser.add_argument("--version", action="version", version=f"boswas-text-editor {__version__}")
    return parser.parse_known_args(argv)


def forwarded(options: argparse.Namespace) -> list[str]:
    """What is handed to the running editor: absolute paths (it has another working directory)."""
    arguments = ["--new-window"] if options.new_window else []
    for name in options.files:
        if name.startswith("file://"):
            from urllib.parse import unquote, urlparse
            name = unquote(urlparse(name).path)
        if name and "\x00" not in name:
            arguments.append(os.path.abspath(name))
    return arguments


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    options, rest = parse_arguments(argv[1:])
    program = os.path.basename(argv[0]) if argv else "boswas-text-editor"
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        sys.stderr.write(f"{program}: runs as the desktop user, never as root\n")
        return EXIT_ROOT
    from PySide6.QtWidgets import QApplication

    import boswas_fluent as fluent

    from .app import EditorApplication

    # fluent.run starts one window per program; the editor has several windows in one process, so the
    # same steps are taken here with the pieces fluent.run is made of.
    app = QApplication.instance() or QApplication([program, *rest])
    app.setDesktopFileName(APP_ID)
    app.setApplicationName(APP_ID)
    app.setOrganizationName("Boswas")
    app.setApplicationDisplayName(DISPLAY_NAME)
    arguments = forwarded(options)
    instance = fluent.SingleInstance(APP_ID, app)
    if not instance.start(arguments):
        return 0
    fluent.apply(app)
    icon = fluent.icon("accessories-text-editor")
    if not icon.isNull():
        app.setWindowIcon(icon)
    editor = EditorApplication(fluent.config_dir(APP_ID), fluent.state_dir(APP_ID), app)
    window = editor.new_window(restore=not options.new_window)
    window.open_paths([argument for argument in arguments if argument != "--new-window"])

    def another_start(received: list) -> None:
        target = editor.handle_arguments([str(argument) for argument in received])
        if target is not None:
            fluent.activate_window(target, instance.activation_token)

    instance.message_received.connect(another_start)
    app.aboutToQuit.connect(editor.save_session_now)
    signals = fluent.install_signal_handlers(app)
    try:
        return int(app.exec())
    finally:
        signals.restore()
        instance.close()
        editor.shutdown()


if __name__ == "__main__":
    sys.exit(main())
