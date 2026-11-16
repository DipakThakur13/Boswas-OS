"""Shared pieces of the Files window tests: a private home, services on fakes, a window."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for extra in (str(ROOT), str(ROOT.parents[1] / "desktop" / "ui" / "qt")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from boswas_fluent import testing  # noqa: E402  (must come before anything creates a QApplication)

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from boswas_apps.files.desktopapps import AppIndex  # noqa: E402
from boswas_apps.files.services import Services  # noqa: E402
from boswas_apps.files.volumes import Volume  # noqa: E402
from boswas_apps.files.window import MainWindow  # noqa: E402

_ENVIRONMENT = ("HOME", "XDG_CONFIG_DIRS", "XDG_CURRENT_DESKTOP", "LANG", "LC_ALL")


class FakeLauncher:
    """Records what would be started."""

    def __init__(self, programs=("kate", "viewer", "boswas-terminal")) -> None:
        self.started: list[tuple[str, list[str], str]] = []
        self.programs = set(programs)

    def install(self, launcher) -> None:
        launcher.start = lambda program, arguments, directory: (self.started.append(
            (program, list(arguments), directory)), True)[1]
        launcher.find = lambda name: (name if name.startswith("/") and os.access(name, os.X_OK)
                                      else f"/usr/bin/{name}" if name in self.programs else None)


class FilesCase(testing.FluentTestCase):
    """A test with a home directory under ``self.tmp`` and a ``Services`` that touches nothing else."""

    def setUp(self) -> None:
        super().setUp()
        self._saved = {name: os.environ.get(name) for name in _ENVIRONMENT}
        self.home = str(self.tmp / "home")
        os.makedirs(self.home)
        os.environ["HOME"] = self.home
        os.environ["XDG_CONFIG_DIRS"] = str(self.tmp / "xdg")
        os.environ["LANG"] = "C.UTF-8"
        os.environ.pop("XDG_CURRENT_DESKTOP", None)
        os.environ.pop("LC_ALL", None)
        self.docs = self.mkdir("home/Documents")
        self.mkdir("home/Downloads")
        self.services_list: list[Services] = []
        self.launcher = FakeLauncher()
        QGuiApplication.clipboard().clear()

    def tearDown(self) -> None:
        for services in self.services_list:
            services.shutdown()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        for directory, names, _files in os.walk(self.tmp):
            for name in names:
                path = os.path.join(directory, name)
                if not os.path.islink(path):
                    os.chmod(path, 0o700)
        super().tearDown()

    # --- files ---
    def path(self, *parts: str) -> str:
        return os.path.join(str(self.tmp), *parts)

    def mkdir(self, relative: str) -> str:
        os.makedirs(self.path(relative), exist_ok=True)
        return self.path(relative)

    def write(self, relative: str, content: bytes | str = b"x", mode: int | None = None) -> str:
        target = self.path(relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(content.encode() if isinstance(content, str) else content)
        if mode is not None:
            os.chmod(target, mode)
        return target

    def read(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    # --- application ---
    def services(self, bus=None, volumes: list[Volume] | None = None) -> Services:
        services = Services(bus=bus, settings_path=self.path("home/.config/files/settings.json"))
        self.launcher.install(services.launcher)
        shown = list(volumes or [])
        if bus is None:
            services.volumes._build = lambda *_args, **_kwargs: list(shown)
        self.services_list.append(services)
        # The applications of the test only: the system's own would make the tests depend on the machine.
        user_list = os.path.join(os.environ["XDG_CONFIG_HOME"], "mimeapps.list")
        services.new_index = lambda: AppIndex(app_dirs=[self.path("share/applications")], list_files=[user_list],
                                              user_list=user_list)
        services.apps = services.new_index()
        services.apps.load()
        services.volumes.refresh()
        services.worker.wait(5)
        return services

    def window(self, location: str | None = None, services: Services | None = None, **kwargs) -> MainWindow:
        services = services or self.services()
        window = MainWindow(services, kwargs.pop("app", None), [location or self.docs], **kwargs)
        window.ask = lambda *args, **kw: self.answers.pop(0) if self.answers else False
        self.show(window, 1100, 720)
        self.settle(window)
        return window

    answers: list[bool] = []

    def settle(self, window: MainWindow, extra_ms: int = 0) -> None:
        """Wait until every tab is read, every job and every background call has finished."""
        services = window.services
        for _round in range(6):
            services.jobs.wait(15)
            services.worker.wait(10)
            for tab in window.all_tabs():
                tab.model.wait(15)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            testing.process(5)
            pending = any(tab._refresh.isActive() or tab.model.is_loading for tab in window.all_tabs())
            if not pending and not services.jobs.busy() and not services.worker.pending():
                break
            if any(tab._refresh.isActive() for tab in window.all_tabs()):
                testing.wait_until(lambda: not any(tab._refresh.isActive() for tab in window.all_tabs()), 3)
        if extra_ms:
            testing.process(extra_ms)

    def names(self, window: MainWindow) -> list[str]:
        return [entry.name for entry in window.tab().model.rows]

    def select(self, window: MainWindow, *names: str) -> None:
        tab = window.tab()
        base = tab.location
        found = tab.view.select_paths([os.path.join(base, name) for name in names])
        self.assertEqual(found, len(names), names)
        testing.process()

    def menu_texts(self, menu) -> list[str]:
        return [action.text() for action in menu.actions() if not action.isSeparator() and action.isVisible()]

    def trigger(self, menu, text: str) -> None:
        for action in menu.actions():
            if action.text() == text:
                self.assertTrue(action.isEnabled(), text)
                action.trigger()
                testing.process()
                return
        self.fail(f"no menu entry {text!r} in {self.menu_texts(menu)}")
