"""The application: its windows, what a start of the program asks for, the FileManager1 service."""

from __future__ import annotations

import argparse
import contextlib
import io
import os

from PySide6.QtCore import ClassInfo, QObject, Slot

import boswas_fluent as fluent

from . import DISPLAY_NAME, PROGRAM, places
from .clipformats import uri_to_path
from .services import Services, default_location
from .window import MainWindow

FILE_MANAGER_SERVICE = "org.freedesktop.FileManager1"
FILE_MANAGER_PATH = "/org/freedesktop/FileManager1"
_MAX_ITEMS = 64


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROGRAM, description=f"{DISPLAY_NAME}: the file manager of Boswas OS.")
    parser.add_argument("--new-window", action="store_true", help="open a new window")
    parser.add_argument("--select", action="append", default=[], metavar="PATH",
                        help="open the folder of PATH with PATH selected (may be given more than once)")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument("locations", nargs="*", metavar="FOLDER",
                        help="folders to open, as paths or file: URLs; trash:/// and computer:/// are accepted")
    return parser


def to_location(text: str, base: str | None = None) -> str | None:
    """A command line argument or URL as a location; None when it is not one."""
    try:
        return places.normalize(text, base or os.getcwd())
    except (ValueError, OSError):
        return None


def absolute_arguments(arguments: list[str]) -> list[str]:
    """The arguments with relative paths made absolute, for handing them to the running instance."""
    result = []
    for argument in arguments:
        if argument.startswith("-") or ":" in argument.split("/", 1)[0] or argument.startswith(("/", "~")):
            result.append(argument)
        else:
            result.append(os.path.abspath(argument))
    return result


class FilesApp(QObject):
    """Owns the windows of the running instance."""

    def __init__(self, services: Services, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self.windows: list[MainWindow] = []
        self.activation_token = ""

    # --- windows ---
    def new_window(self, locations: list[str] | None = None, select: list[str] | None = None) -> MainWindow:
        window = MainWindow(self.services, self, locations or [default_location(self.services.settings)], select)
        self.windows.append(window)
        window.show()
        fluent.activate_window(window, self.activation_token)
        return window

    def window_closed(self, window: MainWindow) -> None:
        if window in self.windows:
            self.windows.remove(window)

    def active_window(self) -> MainWindow | None:
        for window in self.windows:
            if window.isActiveWindow():
                return window
        return self.windows[-1] if self.windows else None

    def _show(self, location: str, select: list[str] | None = None) -> None:
        window = self.active_window()
        if window is None:
            self.new_window([location], select)
            return
        for tab in window.all_tabs():
            if tab.location == location and not tab.search_query:
                window.tabs.setCurrentIndex(window.all_tabs().index(tab))
                if select:
                    tab.refresh(select=select)
                break
        else:
            window.add_tab(location, select=select)
        fluent.activate_window(window, self.activation_token)

    # --- what a start of the program asks for ---
    def handle_arguments(self, arguments: list[str], first: bool = False) -> None:
        """Do what a command line asks: at the first start, and for every later start of the program."""
        try:
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                options, _unknown = build_parser().parse_known_args([str(item) for item in arguments[:256]])
        except SystemExit:                       # a request that does not parse: treat it as a plain start
            options = build_parser().parse_args([])
        locations = [location for location in (to_location(item) for item in options.locations[:_MAX_ITEMS])
                     if location is not None]
        selections = [location for location in (to_location(item) for item in options.select[:_MAX_ITEMS])
                      if location is not None and not places.is_virtual(location)]
        if first or options.new_window or not self.windows:
            folders = locations or ([os.path.dirname(selections[0])] if selections else None)
            window = self.new_window(folders, None)
            if selections:
                self._select_in(window, selections, reuse_first=not locations)
            return
        if not locations and not selections:
            window = self.active_window()
            if window is not None:
                fluent.activate_window(window, self.activation_token)
            return
        for location in locations:
            self._show(location)
        if selections:
            self.show_items(selections)

    def _select_in(self, window: MainWindow, paths: list[str], reuse_first: bool) -> None:
        groups: dict[str, list[str]] = {}
        for path in paths:
            groups.setdefault(os.path.dirname(path) or "/", []).append(path)
        for number, (folder, items) in enumerate(groups.items()):
            if number == 0 and reuse_first:
                window.tab().navigate(folder, record=False, select=items)
            else:
                window.add_tab(folder, select=items)

    # --- org.freedesktop.FileManager1 ---
    def show_folders(self, paths: list[str]) -> None:
        for path in paths[:_MAX_ITEMS]:
            self._show(path)

    def show_items(self, paths: list[str]) -> None:
        """Open the folders of ``paths`` with the items selected."""
        groups: dict[str, list[str]] = {}
        for path in paths[:_MAX_ITEMS]:
            groups.setdefault(os.path.dirname(path.rstrip("/")) or "/", []).append(path.rstrip("/") or "/")
        for folder, items in groups.items():
            self._show(folder, items)

    def show_item_properties(self, paths: list[str]) -> None:
        window = self.active_window() or self.new_window()
        for path in paths[:8]:
            window.show_properties([path])
        if len(paths) > 8:
            window.show_properties(paths[8:_MAX_ITEMS + 8])


def _paths(uris: list[str]) -> list[str]:
    paths = []
    for uri in list(uris)[:_MAX_ITEMS * 4]:
        if not isinstance(uri, str) or len(uri) > 8192:
            continue
        path = uri_to_path(uri) if ":" in uri.split("/", 1)[0] else (uri if uri.startswith("/") else None)
        if path is not None:
            paths.append(os.path.normpath(path))
    return paths


@ClassInfo({"D-Bus Interface": FILE_MANAGER_SERVICE})
class FileManagerService(QObject):
    """``org.freedesktop.FileManager1``: what other applications call for "Show in folder"."""

    def __init__(self, app: FilesApp) -> None:
        super().__init__(app)
        self._app = app

    @Slot("QStringList", str)
    def ShowFolders(self, uris, startup_id) -> None:        # noqa: N802 (the D-Bus method names)
        self._app.activation_token = startup_id if isinstance(startup_id, str) else ""
        self._app.show_folders(_paths(uris))

    @Slot("QStringList", str)
    def ShowItems(self, uris, startup_id) -> None:          # noqa: N802
        self._app.activation_token = startup_id if isinstance(startup_id, str) else ""
        self._app.show_items(_paths(uris))

    @Slot("QStringList", str)
    def ShowItemProperties(self, uris, startup_id) -> None:     # noqa: N802
        self._app.activation_token = startup_id if isinstance(startup_id, str) else ""
        self._app.show_item_properties(_paths(uris))


def register_file_manager(app: FilesApp) -> FileManagerService | None:
    """Offer FileManager1 on the session bus; None when there is no bus or another file manager owns the name."""
    try:
        from PySide6.QtDBus import QDBusConnection
    except ImportError:
        return None
    if not (os.environ.get("DBUS_SESSION_BUS_ADDRESS") or fluent.runtime_dir() is not None):
        return None
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        return None
    service = FileManagerService(app)
    if not bus.registerObject(FILE_MANAGER_PATH, service, QDBusConnection.RegisterOption.ExportAllSlots):
        return None
    if not bus.registerService(FILE_MANAGER_SERVICE):
        bus.unregisterObject(FILE_MANAGER_PATH)
        return None
    return service
