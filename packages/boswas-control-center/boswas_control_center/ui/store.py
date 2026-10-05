"""Shared, lazily loaded data of the window.

Several pages show the same facts (the device agent's status appears on
Security, Privacy, System and About), so the data is loaded once per key,
on the Runner's thread pool, when the first page that needs it is shown, and
again on Refresh. Pages listen to ``changed(key)`` and redraw from
``get(key)``.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from ..errors import is_unavailable, user_message


@dataclass(frozen=True)
class Result:
    ok: bool
    value: object = None
    error: str | None = None
    unavailable: bool = False      # a service is not running or a program is not installed


class Store(QObject):
    changed = Signal(str)

    def __init__(self, backend, runner, parent: QObject | None = None):
        super().__init__(parent)
        self.backend, self.runner = backend, runner
        self.loaders = {
            "live": backend.live_session,
            "release": backend.release,
            "hardware": backend.hardware,
            "plasma": backend.plasma_version,
            "storage": backend.storage,
            "power": backend.power,
            "security": backend.security_status,
            "agent": backend.agent_status,
            "agent_config": backend.agent_config,
            "agent_runtime": backend.agent_runtime,
            "session_system": backend.session_system,
            "windows_apps": backend.windows_apps,
            "presets": backend.presets,
            "desktop_apps": backend.desktop_entries,
            "updates": backend.updates,
            "kcms": backend.kcm_availability,
            "programs": backend.program_availability,
        }
        self._results: dict[str, Result] = {}

    def request(self, key: str, force: bool = False) -> None:
        if key not in self.loaders:
            raise KeyError(key)
        if (key in self._results and not force) or self.runner.busy(f"store:{key}"):
            return
        self.runner.submit(self.loaders[key], key=f"store:{key}",
                           on_done=lambda value, k=key: self._set(k, Result(True, value)),
                           on_error=lambda exc, k=key: self._set(k, Result(False, None, user_message(exc),
                                                                           is_unavailable(exc))))

    def _set(self, key: str, result: Result) -> None:
        self._results[key] = result
        self.changed.emit(key)

    def get(self, key: str) -> Result | None:
        return self._results.get(key)

    def value(self, key: str, default=None):
        result = self._results.get(key)
        return result.value if result is not None and result.ok else default

    def error(self, key: str) -> str | None:
        result = self._results.get(key)
        return result.error if result is not None and not result.ok else None

    def loading(self, key: str) -> bool:
        return key not in self._results or self.runner.busy(f"store:{key}")
