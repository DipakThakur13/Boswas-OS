"""boswas-files: start the file manager, or hand the request to the instance that is running."""

from __future__ import annotations

import locale
import os
import sys

from . import APP_ID, DISPLAY_NAME, PROGRAM

EXIT_ROOT = 4


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    from .app import absolute_arguments, build_parser
    options = build_parser().parse_args(argv[1:])           # --help and wrong options end here
    if options.version:
        from boswas_apps import __version__
        print(f"{PROGRAM} {__version__}")
        return 0
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        sys.stderr.write(f"{PROGRAM}: runs as the desktop user, never as root\n")
        return EXIT_ROOT
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    arguments = absolute_arguments(argv[1:])

    from PySide6.QtWidgets import QApplication

    import boswas_fluent as fluent

    app = QApplication.instance() or QApplication(argv[:1])
    app.setDesktopFileName(APP_ID)
    app.setApplicationName(APP_ID)
    app.setOrganizationName("Boswas")
    app.setApplicationDisplayName(DISPLAY_NAME)
    instance = fluent.SingleInstance(APP_ID, app)
    if not instance.start(arguments):
        return 0                                # the running instance does what was asked
    fluent.apply(app)
    icon = fluent.icon("boswas-files")
    if not icon.isNull():
        app.setWindowIcon(icon)

    from .app import FilesApp, register_file_manager
    from .services import Services

    bus = None
    try:
        from PySide6.QtDBus import QDBusConnection

        from .udisks import UDisksBus
        if QDBusConnection.systemBus().isConnected():
            bus = UDisksBus(parent=app)
    except ImportError:
        bus = None
    services = Services(app, bus=bus)
    files = FilesApp(services, app)

    def another_start(forwarded: list) -> None:
        files.activation_token = instance.activation_token
        files.handle_arguments([str(item) for item in forwarded])

    instance.message_received.connect(another_start)
    register_file_manager(files)
    signals = fluent.install_signal_handlers(app)
    services.start()
    files.handle_arguments(arguments, first=True)
    try:
        return int(app.exec())
    finally:
        signals.restore()
        services.shutdown()
        instance.close()


if __name__ == "__main__":
    sys.exit(main())
