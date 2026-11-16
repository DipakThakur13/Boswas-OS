"""Where Clock, Calendar and the reminder service keep their files (standard library only).

The same directories ``boswas_fluent.config_dir`` / ``data_dir`` /
``state_dir`` give an application; they are computed here again because
the service does not load the toolkit.

  $XDG_DATA_HOME/com.boswas.Clock/clock.json          alarms, timers, focus session, stopwatch, world clock
  $XDG_DATA_HOME/com.boswas.Calendar/calendar.json    calendars and events
  $XDG_STATE_HOME/com.boswas.Reminders/state.json     what the service has fired, snoozed and seen
  $XDG_RUNTIME_DIR/boswas-reminders.lock              held by the running service (one per session)
"""

from __future__ import annotations

import os
from pathlib import Path

CLOCK_ID = "com.boswas.Clock"
CALENDAR_ID = "com.boswas.Calendar"
REMINDERS_ID = "com.boswas.Reminders"


def _xdg(variable: str, default: str) -> Path:
    value = os.environ.get(variable) or ""
    return Path(value) if value.startswith("/") else Path.home() / default


def data_home() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def config_home() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def state_home() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state")


def runtime_dir() -> Path | None:
    """``$XDG_RUNTIME_DIR`` when it is a private directory of this user, else None."""
    value = os.environ.get("XDG_RUNTIME_DIR") or ""
    if not value.startswith("/"):
        return None
    try:
        info = os.stat(value)
    except OSError:
        return None
    if not os.path.isdir(value) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        return None
    return Path(value)


def clock_store() -> Path:
    return data_home() / CLOCK_ID / "clock.json"


def clock_settings() -> Path:
    return config_home() / CLOCK_ID / "settings.json"


def calendar_store() -> Path:
    return data_home() / CALENDAR_ID / "calendar.json"


def calendar_settings() -> Path:
    return config_home() / CALENDAR_ID / "settings.json"


def service_state() -> Path:
    return state_home() / REMINDERS_ID / "state.json"


def service_lock() -> Path:
    """The lock file of the running service: in the runtime directory, else beside its state."""
    runtime = runtime_dir()
    if runtime is not None:
        return runtime / "boswas-reminders.lock"
    return state_home() / REMINDERS_ID / "service.lock"


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and its parents) for this user only; returns it."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path
