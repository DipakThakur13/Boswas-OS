"""What the window asks the system for, gathered in one object.

``Backend`` is the only place of the user interface that reaches the real
system: /proc and /sys (through the sampler), the D-Bus services, the
desktop entries, signals, new programs, the clipboard. Tests give the
window a Backend whose root is a fake tree and whose D-Bus clients and
system calls are stand-ins.

Methods that block (D-Bus, reading directories) are called by the pages
on a worker thread; they must not touch widgets.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Mapping

from .. import APP_ID, actions, desktopfile, services
from ..sampler import Sampler
from ..settings import Settings
from .. import settings as settings_module


class Backend:
    def __init__(self, root: str = "/", env: Mapping[str, str] | None = None, *, threaded: bool = True,
                 settings_path: str | os.PathLike | None = None) -> None:
        self.root = root
        self.proc = os.path.join(root, "proc")
        self.env = dict(os.environ if env is None else env)
        self.threaded = threaded
        self.uid = os.getuid()
        self.pid = os.getpid()
        self._settings_path = settings_path
        self._clients: dict[str, object] = {}
        self._logind = None
        self._lock = threading.Lock()
        self.children: list[object] = []           # programs started here, kept to collect them when they end

    # --- sampling ---
    def make_sampler(self) -> Sampler:
        return Sampler(self.root)

    def user_name(self) -> str:
        try:
            import pwd
            return pwd.getpwuid(self.uid).pw_name
        except (ImportError, KeyError, OSError):
            return str(self.uid)

    # --- settings ---
    def settings_path(self) -> Path:
        if self._settings_path is not None:
            return Path(self._settings_path)
        import boswas_fluent as fluent
        return fluent.config_dir(APP_ID) / "settings.json"

    def load_settings(self) -> Settings:
        return settings_module.load(self.settings_path())

    def save_settings(self, settings: Settings) -> None:
        settings_module.save(self.settings_path(), settings)

    # --- processes ---
    def send_signal(self, pid: int, signum: int, start_ticks: int | None, name: str) -> None:
        actions.send_signal(pid, signum, start_ticks, name, proc=self.proc)

    def condition(self, pid: int, start_ticks: int | None) -> str:
        return actions.condition(pid, start_ticks, self.proc)

    def set_priority(self, pid: int, nice: int, start_ticks: int | None, name: str) -> None:
        actions.set_priority(pid, nice, start_ticks, name, proc=self.proc)

    def read_properties(self, pid: int) -> actions.Properties:
        return actions.read_properties(pid, self.proc)

    def start_program(self, arguments: list[str], directory: str) -> None:
        self.children.append(actions.start_program(arguments, directory, search_path=self.env.get("PATH")))

    def show_in_files(self, path: str) -> None:
        self.children.append(actions.show_in_files(path, search_path=self.env.get("PATH")))

    def collect_children(self) -> None:
        """Forget the started programs that have ended (so they do not stay behind as zombies)."""
        self.children = [child for child in self.children if getattr(child, "poll", lambda: 0)() is None]

    def home(self) -> str:
        return self.env.get("HOME") or os.path.expanduser("~")

    # --- desktop entries ---
    def list_applications(self) -> list[desktopfile.Application]:
        return desktopfile.list_applications(self.env)

    def list_startup(self) -> list[desktopfile.StartupEntry]:
        return desktopfile.list_startup(self.env)

    def set_startup_enabled(self, file_id: str, enabled: bool) -> None:
        desktopfile.set_startup_enabled(file_id, enabled, self.env)

    def add_startup(self, application_path: str) -> str:
        return desktopfile.add_startup(application_path, self.env)

    def remove_startup(self, file_id: str) -> None:
        desktopfile.remove_startup(file_id, self.env)

    # --- D-Bus ---
    def _systemd(self, scope: str):
        with self._lock:
            client = self._clients.get(scope)
            if client is None:
                from ..bus import SystemdClient
                client = self._clients[scope] = SystemdClient(scope)
            return client

    def list_services(self, scope: str, full: bool = False) -> list[services.Unit]:
        """The services of the ``system`` or the ``user`` manager. Raises bus.BusError."""
        return self._systemd(scope).list_services(full)

    def service_action(self, scope: str, action: str, name: str) -> str:
        return self._systemd(scope).act(action, name)

    def list_sessions(self) -> list[services.Session]:
        """The sessions logind knows. Raises bus.BusError when logind cannot be asked."""
        with self._lock:
            if self._logind is None:
                from ..bus import LogindClient
                self._logind = LogindClient()
            client = self._logind
        return client.list_sessions()

    # --- clipboard ---
    def copy_text(self, text: str) -> None:
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(text)
