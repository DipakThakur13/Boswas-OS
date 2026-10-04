#!/usr/bin/env python3
"""Drive the real Compatibility Manager window (Qt offscreen) against the real
session agent, as the desktop user. Prints one JSON document with what the
window showed and did. Used by device_scenario.py.

  gui_probe.py APPLICATION_ID
"""

import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, "/usr/lib/boswas/python")

from PySide6.QtWidgets import QApplication  # noqa: E402

from boswas_manager.backend import Backend  # noqa: E402
from boswas_manager.ui.main_window import MainWindow  # noqa: E402


def pump(app, seconds: float, until=None) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.05)
    return until() if until else True


def state_of(app_id: str) -> str | None:
    apps = {a.get("id"): a for a in Backend().list_apps()}
    return (apps.get(app_id) or {}).get("app_state")


def main() -> int:
    app_id = sys.argv[1]
    app = QApplication([])
    window = MainWindow(Backend())
    # Modal interactions go through the window's hooks; nobody is there to click a dialog.
    errors: list[str] = []
    window.ask = lambda *_args, **_kwargs: True                        # confirmations
    window.notify_error = lambda title, text: errors.append(f"{title}: {text}")
    window.dialog_exec = lambda dialog: errors.append(f"unexpected dialog {type(dialog).__name__}") or 0
    window.show()
    window.refresh(full=True)
    result = {"errors": errors}

    def emit(**values) -> None:
        """Print the results so far: if a step hangs, the caller still sees the earlier ones."""
        result.update(values)
        print(json.dumps(result), flush=True)

    emit(listed=pump(app, 90, lambda: app_id in window.library.visible_ids()),
         visible=window.library.visible_ids())
    pump(app, 60, lambda: bool(window.system))
    system = window.system or {}
    emit(system_loaded=bool(system), agent_state=(system.get("agent") or {}).get("state"),
         windows_architectures=system.get("windows_architectures"))
    window.launch_app(app_id)
    emit(launched=pump(app, 120, lambda: state_of(app_id) == "RUNNING"))
    window.stop_app(app_id)
    emit(stopped=pump(app, 120, lambda: state_of(app_id) == "STOPPED"))
    window.shutdown()
    os._exit(0)


if __name__ == "__main__":
    main()
