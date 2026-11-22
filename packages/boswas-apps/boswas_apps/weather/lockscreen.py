"""The weather on the lock screen: the summary file, and the update that runs without the window (no Qt).

The lock screen (``boswas-shell --lock``) never talks to the network. It shows a summary of the home place's
forecast (or of the first place, when no place is home) that Weather keeps in

    $XDG_STATE_HOME/boswas/weather.json         (~/.local/state/boswas/weather.json)

next to the shell's other state files. Weather writes it whenever it has a forecast of that place, and
``boswas-weather --update-lock-screen`` (run by the systemd user timer ``boswas-weather-update.timer``) fetches
the forecast and writes it while the window is closed. Python decides every word and every icon; the lock screen
only checks the values and shows them::

    {"version": 1, "place": "Berlin", "fetched": 1763731200, "unit": "c",
     "temperature": "8°C", "condition": "Partly cloudy", "icon": "weather-few-clouds-symbolic",
     "updated": "Updated at 14:20", "feels_like": "Feels like 5°", "high_low": "High 9° · Low 3°",
     "today_until": 1763766000,
     "hours": [{"at": 1763733600, "time": "15:00", "icon": "weather-overcast-symbolic", "temperature": "8°"}]}

``fetched``, ``today_until`` and ``at`` are seconds since the epoch: the lock screen hides a summary older than
three hours, says "Updated at ..." when it is more than an hour old, drops today's high and low when the place's
day is over, and shows the four hours that begin after the present moment. The file is written atomically with
mode 0600 and is at most ``MAX_BYTES``; ``feels_like``, ``high_low`` and ``today_until`` are left out when the
forecast does not have them.

Whether the lock screen shows the weather is the compositor's setting ``[lock] show_weather`` (on unless a
configuration file turns it off; Settings > Privacy & security > Lock screen). It is read here from the same
three files as the compositor reads; when it is off nothing is fetched and the summary is removed.

Privacy: nothing is fetched for the lock screen unless the user added a place in Weather, and only that place's
rounded coordinates are sent, as when the window asks (see ``service``). There is no location lookup.
"""

from __future__ import annotations

import datetime as dt
import locale
import os
import stat
import sys
import time
import tomllib
from pathlib import Path
from typing import Callable, Mapping

from . import APP_ID, model, storage
from .model import Place
from .places import PlaceList
from .service import ForecastCache, Result, ServiceError, WeatherService
from .settings import Settings

VERSION = 1
FILE_NAME = "weather.json"
MAX_BYTES = 16 * 1024                   # the lock screen reads no more
HOURS = 12                              # hours written: enough for the three hours the summary is shown
FRESH_SECONDS = 10 * 60                 # a cached forecast this young is used instead of asking the service

# The compositor's configuration layers (boswas-compositor(1)); the user's file is added by compositor_files().
SYSTEM_FILES = ("/usr/share/boswas/compositor/compositor.toml", "/etc/boswas/compositor.toml")
MAX_CONFIG_BYTES = 256 * 1024
# The territories whose locales use the units of the United States (as Qt's ImperialUSSystem).
IMPERIAL_TERRITORIES = frozenset({"US", "LR", "MM"})


# --- where things are ------------------------------------------------------------------------------------

def _base(environ: Mapping[str, str], variable: str, fallback: str) -> Path | None:
    """An XDG base directory: the variable when it is an absolute path, else ``$HOME/<fallback>``."""
    value = environ.get(variable) or ""
    if value.startswith("/") and "\0" not in value:
        return Path(value)
    home = environ.get("HOME") or ""
    if home.startswith("/") and "\0" not in home:
        return Path(home) / fallback
    return None


def summary_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """``$XDG_STATE_HOME/boswas/weather.json``, where the lock screen looks (as the shell finds its state)."""
    base = _base(os.environ if environ is None else environ, "XDG_STATE_HOME", ".local/state")
    return base / "boswas" / FILE_NAME if base is not None else None


def app_config_dir(environ: Mapping[str, str] | None = None) -> Path | None:
    """Weather's configuration directory (places.json, settings.json), as boswas_fluent.config_dir finds it."""
    base = _base(os.environ if environ is None else environ, "XDG_CONFIG_HOME", ".config")
    return base / APP_ID if base is not None else None


def app_cache_dir(environ: Mapping[str, str] | None = None) -> Path | None:
    """Weather's cache directory (the forecasts), as boswas_fluent.cache_dir finds it."""
    base = _base(os.environ if environ is None else environ, "XDG_CACHE_HOME", ".cache")
    return base / APP_ID if base is not None else None


def compositor_files(environ: Mapping[str, str] | None = None) -> list[str]:
    """The compositor's configuration files, in the order it merges them: shipped, administrator, user."""
    files = list(SYSTEM_FILES)
    base = _base(os.environ if environ is None else environ, "XDG_CONFIG_HOME", ".config")
    if base is not None:
        files.append(str(base / "boswas" / "compositor.toml"))
    return files


# --- the setting -----------------------------------------------------------------------------------------

def _read_toml(path: str) -> dict | None:
    """A configuration file as the compositor would take it, or None (missing, too large, not TOML)."""
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
            return None
        with open(path, "rb") as handle:
            data = handle.read(MAX_CONFIG_BYTES + 1)
        return tomllib.loads(data.decode("utf-8")) if len(data) <= MAX_CONFIG_BYTES else None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError):
        return None


def show_weather(files: list[str] | None = None, environ: Mapping[str, str] | None = None) -> bool:
    """``[lock] show_weather`` of the compositor's configuration: true unless a later file sets it to false.

    A file that cannot be read, or whose value is not a boolean, says nothing. (The compositor ignores a file
    with a mistake anywhere in it as a whole; such a difference only ever leaves the weather off the lock
    screen, because the lock screen itself follows the compositor.)
    """
    shown = True
    for path in compositor_files(environ) if files is None else files:
        document = _read_toml(path)
        table = document.get("lock") if isinstance(document, dict) else None
        value = table.get("show_weather") if isinstance(table, dict) else None
        if isinstance(value, bool):
            shown = value
    return shown


# --- the user's choices without Qt ------------------------------------------------------------------------

def _locale_name(environ: Mapping[str, str], category: str) -> str:
    for variable in ("LC_ALL", category, "LANG"):
        value = environ.get(variable) or ""
        if value:
            return value
    return ""


def locale_imperial(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the locale's measurements are those of the United States ("en_US.UTF-8", "my_MM")."""
    name = _locale_name(os.environ if environ is None else environ, "LC_MEASUREMENT")
    territory = name.split(".", 1)[0].split("@", 1)[0].partition("_")[2]
    return territory.upper() in IMPERIAL_TERRITORIES


def twelve_hour_format(time_format: str) -> bool:
    """Whether a C library time format ("%r", "%H:%M:%S", "%I:%M %p") is a 12-hour clock."""
    return any(code in time_format for code in ("%I", "%l", "%p", "%r"))


def locale_twelve_hour() -> bool:
    """Whether the user's locale (LC_TIME) writes times with the 12-hour clock."""
    try:
        previous = locale.setlocale(locale.LC_TIME)
        try:
            locale.setlocale(locale.LC_TIME, "")
            return twelve_hour_format(locale.nl_langinfo(locale.T_FMT))
        finally:
            locale.setlocale(locale.LC_TIME, previous)
    except (locale.Error, AttributeError, ValueError):
        return False


def twelve_hour(settings: Settings, locale_answer: Callable[[], bool] = locale_twelve_hour) -> bool:
    """The clock of Weather's settings; "locale" asks the locale (the window asks Qt the same question)."""
    if settings.time_format in ("12", "24"):
        return settings.time_format == "12"
    return locale_answer()


def lock_place(places: PlaceList) -> Place | None:
    """The place the lock screen shows: the home place, else the first place, else None."""
    return places.home_place() or (places.places[0] if places.places else None)


# --- the summary -----------------------------------------------------------------------------------------

def _epoch(moment: dt.datetime, utc_offset: int) -> int:
    """A naive local time of the place (as in the forecast) as seconds since the epoch."""
    return int((moment - dt.datetime(1970, 1, 1)).total_seconds()) - utc_offset


def build_summary(place: Place, result: Result, settings: Settings, twelve: bool, now: float,
                  zone: dt.tzinfo | None = None) -> dict | None:
    """The summary of ``place``'s forecast at ``now``; None when the forecast has no temperature for now.

    ``zone`` is the time zone "Updated at" is written in (default: the local one, the lock screen's clock).
    """
    forecast = result.forecast
    unit = settings.temperature
    local_now = forecast.local_now(now)
    current = forecast.conditions(local_now)
    if current is None or current.temperature is None:
        return None
    summary: dict = {
        "version": VERSION,
        "place": place.name,
        "fetched": int(result.fetched),
        "unit": unit,
        "temperature": model.format_temperature(current.temperature, unit, with_unit=True),
        "condition": model.condition_text(current.code, current.is_day),
        "icon": model.condition_icon(current.code, current.is_day),
        "updated": model.updated_text(result.fetched, now, twelve, zone),
    }
    if current.apparent is not None:
        summary["feels_like"] = f"Feels like {model.format_temperature(current.apparent, unit)}"
    today = forecast.day(local_now.date())
    if today is not None and today.temperature_max is not None and today.temperature_min is not None:
        summary["high_low"] = (f"High {model.format_temperature(today.temperature_max, unit)} · "
                               f"Low {model.format_temperature(today.temperature_min, unit)}")
        midnight = dt.datetime.combine(today.date + dt.timedelta(days=1), dt.time())
        summary["today_until"] = _epoch(midnight, forecast.utc_offset)
    summary["hours"] = [
        {"at": _epoch(hour.time, forecast.utc_offset), "time": model.format_hour(hour.time, twelve),
         "icon": model.condition_icon(hour.code, hour.is_day),
         "temperature": model.format_temperature(hour.temperature, unit)}
        for hour in forecast.hours_from(local_now, HOURS) if hour.temperature is not None]
    return summary


def encode(summary: dict) -> bytes:
    """The file's bytes; ValueError when the summary would be larger than the lock screen reads."""
    import json
    data = (json.dumps(summary, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError(f"the summary would be {len(data)} bytes, more than the lock screen reads")
    return data


def write_summary(path: Path, summary: dict) -> None:
    """Replace the summary in one step (a private file beside it, renamed over it): mode 0600, directory 0700."""
    storage.write_bytes_atomic(path, encode(summary))


def remove_summary(path: Path | None) -> None:
    if path is not None:
        try:
            storage.remove(path)
        except OSError:
            pass


def sync(path: Path | None, place: Place | None, result: Result | None, settings: Settings, twelve: bool,
         now: float, *, shown: bool) -> bool:
    """Bring the summary in line with the window's state: written for ``place``'s forecast; removed when the
    setting is off, when there is no place, and when that place's forecast is not known (the summary would
    be of a place that is no longer the lock screen's; its first forecast writes it). Returns whether a
    summary was written. (The window calls this on its files thread.)"""
    if path is None:
        return False
    wanted = shown and place is not None and result is not None
    summary = build_summary(place, result, settings, twelve, now) if wanted else None
    if summary is None:
        remove_summary(path)
        return False
    write_summary(path, summary)
    return True


# --- the update without the window -----------------------------------------------------------------------

def _load_places(directory: Path) -> PlaceList:
    """The user's places, read only: a damaged file is the window's to set aside and to tell about."""
    from . import places
    try:
        return PlaceList.from_json(storage.read_json(directory / places.FILE_NAME, places.MAX_BYTES))
    except (storage.StoreError, RecursionError):
        return PlaceList()


def _load_settings(directory: Path, environ: Mapping[str, str]) -> Settings:
    from . import settings
    base = Settings.imperial() if locale_imperial(environ) else Settings()
    return settings.load(directory / settings.FILE_NAME, base)[0]


def update(environ: Mapping[str, str] | None = None, *, fetch: Callable[[str], bytes] | None = None,
           clock: Callable[[], float] = time.time, files: list[str] | None = None,
           locale_answer: Callable[[], bool] = locale_twelve_hour, log=None) -> int:
    """``boswas-weather --update-lock-screen``: fetch the lock screen's place and write the summary.

    Quietly does nothing more and returns 0 when the setting is off (the summary is removed), when the user has
    no place (no network at all; a summary of a place that is gone is removed), and when the service cannot be
    reached (the summary stays as it is). Returns 1 when the summary cannot be written.
    """
    environ = os.environ if environ is None else environ
    log = log if log is not None else sys.stderr
    path = summary_path(environ)
    if path is None:
        return 0
    if not show_weather(files, environ):
        remove_summary(path)
        return 0
    config = app_config_dir(environ)
    place = lock_place(_load_places(config)) if config is not None else None
    if place is None:
        remove_summary(path)
        return 0
    settings = _load_settings(config, environ)
    cache_dir = app_cache_dir(environ)
    service = WeatherService(fetch=fetch, cache=ForecastCache(cache_dir) if cache_dir is not None else None,
                             clock=clock)
    result = service.cached(place)
    if result is None or not 0 <= clock() - result.fetched < FRESH_SECONDS:
        try:
            result = service.forecast(place)      # also renews Weather's cache
        except ServiceError:
            return 0                              # offline: the last summary stays until it is too old to show
    now = clock()
    summary = build_summary(place, result, settings, twelve_hour(settings, locale_answer), now)
    try:
        if summary is None:
            remove_summary(path)
        else:
            write_summary(path, summary)
    except (OSError, ValueError) as exc:
        log.write(f"boswas-weather: the weather for the lock screen could not be saved: {exc}\n")
        return 1
    return 0
