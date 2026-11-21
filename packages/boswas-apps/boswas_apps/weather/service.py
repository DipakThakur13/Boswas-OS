"""The weather service (Open-Meteo): addresses, the one HTTP call, the forecast cache (standard library only).

Weather sends Open-Meteo nothing but what a question needs: the text typed into the place search, or
the coordinates of a place (rounded to four decimals, about 10 m), with a fixed User-Agent. There is
no account, key, cookie or identifier. Answers are capped at ``MAX_BYTES``, redirects are refused and
everything that comes back goes through ``model.parse_places`` / ``model.parse_forecast``.

``WeatherService.fetch`` is the HTTP call: a function ``fetch(url) -> bytes`` that raises
``ServiceError``. Tests give the service their own function and so answer every request themselves.
The service's methods block: the window calls them on a worker thread.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import model, storage
from .model import Place

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HOSTS = frozenset({"geocoding-api.open-meteo.com", "api.open-meteo.com"})
USER_AGENT = "boswas-weather/1.0"
TIMEOUT = 15.0                         # seconds
MAX_BYTES = 2 * 1024 * 1024
MIN_QUERY, MAX_QUERY = 2, 100
SEARCH_COUNT = 10
FORECAST_DAYS = 10

CURRENT_FIELDS = ("temperature_2m", "relative_humidity_2m", "apparent_temperature", "is_day", "precipitation",
                  "weather_code", "cloud_cover", "pressure_msl", "wind_speed_10m", "wind_direction_10m",
                  "wind_gusts_10m", "visibility", "uv_index")
HOURLY_FIELDS = ("temperature_2m", "apparent_temperature", "precipitation_probability", "precipitation",
                 "weather_code", "is_day", "wind_speed_10m")
DAILY_FIELDS = ("weather_code", "temperature_2m_max", "temperature_2m_min", "apparent_temperature_max",
                "apparent_temperature_min", "sunrise", "sunset", "uv_index_max", "precipitation_sum",
                "precipitation_probability_max", "wind_speed_10m_max", "wind_direction_10m_dominant")


class ServiceError(Exception):
    """A question the weather service did not answer usefully. ``str()`` is a sentence for the user."""
    title = "Could not get the weather"


class NetworkError(ServiceError):
    """The service could not be reached (no network, a timeout, a refused connection)."""
    title = "Could not reach the weather service"


class ResponseError(ServiceError):
    """The service answered, but with an error or with data that cannot be used."""
    title = "The weather service did not answer as expected"


# --- addresses ---------------------------------------------------------------------------------------

def search_query(text: str) -> str:
    """The search text as it is sent: one line, at most MAX_QUERY characters; "" when too short."""
    query = model.clean_text(text, MAX_QUERY)
    return query if len(query) >= MIN_QUERY else ""


def search_url(text: str) -> str:
    query = search_query(text)
    if not query:
        raise ValueError(f"a place search needs at least {MIN_QUERY} characters")
    return GEOCODING_URL + "?" + urllib.parse.urlencode(
        {"name": query, "count": SEARCH_COUNT, "language": "en", "format": "json"})


def forecast_url(latitude: float, longitude: float) -> str:
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("coordinates out of range")
    parameters = [("latitude", f"{latitude:.4f}"), ("longitude", f"{longitude:.4f}"), ("timezone", "auto"),
                  ("forecast_days", str(FORECAST_DAYS)), ("current", ",".join(CURRENT_FIELDS)),
                  ("hourly", ",".join(HOURLY_FIELDS)), ("daily", ",".join(DAILY_FIELDS))]
    return FORECAST_URL + "?" + urllib.parse.urlencode(parameters, safe=",")


# --- the HTTP call -----------------------------------------------------------------------------------

class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):     # noqa: N802 (urllib's name)
        return None                    # urllib then raises HTTPError for the 3xx answer


def http_get(url: str, *, timeout: float = TIMEOUT, max_bytes: int = MAX_BYTES) -> bytes:
    """GET an address of the weather service; the body, at most ``max_bytes``. Raises ServiceError."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in HOSTS:
        raise ValueError("not an address of the weather service")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    opener = urllib.request.build_opener(_NoRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > max_bytes:
                raise ResponseError("The weather service sent more data than Weather accepts.")
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        reason = ""
        try:
            reason = model.service_reason(json.loads(exc.read(16 * 1024).decode("utf-8")))
        except (OSError, ValueError, UnicodeDecodeError, RecursionError, AttributeError):
            pass
        text = f"The weather service answered with an error (HTTP {exc.code})."
        raise ResponseError(f"{text} {reason}" if reason else text) from exc
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        raise NetworkError("Check your connection, then try again.") from exc
    if len(data) > max_bytes:
        raise ResponseError("The weather service sent more data than Weather accepts.")
    return data


def decode(data: bytes):
    """The JSON value of an answer; raises ResponseError."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_BYTES:
        raise ResponseError("The weather service sent more data than Weather accepts.")
    try:
        return json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ResponseError("The weather service sent data that could not be read.") from exc


# --- the cache ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Result:
    forecast: model.Forecast
    fetched: float                     # when it was fetched, in seconds since the epoch
    cached: bool                       # read from the cache (Weather is showing what it knew before)


_KEY = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}")


class ForecastCache:
    """The last forecast of every place, one file each: ``forecast-<key>.json`` in Weather's cache directory.

    A file holds the answer exactly as the service sent it (metric), when it was fetched and for which
    place: ``{"version": 1, "key": ..., "fetched": ..., "data": <answer>}``.
    """

    VERSION = 1

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def path(self, key: str) -> Path:
        if not _KEY.fullmatch(key):
            raise ValueError(f"not a place key: {key!r}")
        return self.directory / f"forecast-{key}.json"

    def write(self, place: Place, data: bytes, fetched: float) -> None:
        head = json.dumps({"version": self.VERSION, "key": place.key, "fetched": fetched})[:-1]
        storage.write_bytes_atomic(self.path(place.key), head.encode("utf-8") + b', "data": ' + bytes(data) + b"}\n")

    def read(self, place: Place, now: float | None = None) -> Result | None:
        """The cached forecast of ``place``, or None (no file, a damaged file, a file of another place)."""
        try:
            raw = storage.read_json(self.path(place.key), MAX_BYTES + 4096)
        except (storage.StoreError, ValueError, RecursionError):
            return None
        if not isinstance(raw, dict) or raw.get("version") != self.VERSION or raw.get("key") != place.key:
            return None
        latest = (time.time() if now is None else now) + 86400
        fetched = model.number(raw.get("fetched"), 0, latest)
        if fetched is None:
            return None
        try:
            return Result(model.parse_forecast(raw.get("data")), fetched, True)
        except model.DataError:
            return None

    def remove(self, key: str) -> None:
        try:
            storage.remove(self.path(key))
        except (OSError, ValueError):
            pass

    def prune(self, keep: set[str]) -> None:
        """Delete the files of places that are no longer kept."""
        try:
            files = list(self.directory.glob("forecast-*.json"))
        except OSError:
            return
        for path in files:
            key = path.name[len("forecast-"):-len(".json")]
            if key not in keep:
                try:
                    storage.remove(path)
                except OSError:
                    pass


# --- the service -------------------------------------------------------------------------------------

class WeatherService:
    """Place search and forecasts; every method blocks (call them on a worker thread)."""

    def __init__(self, fetch: Callable[[str], bytes] | None = None, cache: ForecastCache | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.fetch = fetch if fetch is not None else http_get
        self.cache = cache
        self.clock = clock

    def search(self, text: str) -> list[Place]:
        """The places whose name matches ``text`` (at most SEARCH_COUNT)."""
        raw = decode(self.fetch(search_url(text)))
        try:
            return model.parse_places(raw)[:SEARCH_COUNT]
        except model.DataError as exc:
            raise ResponseError(str(exc)) from exc

    def forecast(self, place: Place) -> Result:
        """The forecast of ``place`` from the service; it is also written to the cache."""
        data = self.fetch(forecast_url(place.latitude, place.longitude))
        raw = decode(data)
        try:
            forecast = model.parse_forecast(raw)
        except model.DataError as exc:
            raise ResponseError(f"The forecast could not be read: {exc}") from exc
        fetched = self.clock()
        if self.cache is not None:
            try:
                self.cache.write(place, data, fetched)
            except (OSError, ValueError):
                pass                   # a forecast that cannot be cached is still shown
        return Result(forecast, fetched, False)

    def cached(self, place: Place) -> Result | None:
        return self.cache.read(place, self.clock()) if self.cache is not None else None

    def cached_many(self, places: list[Place]) -> dict[str, Result]:
        """The cached forecasts of several places (at start-up), by key."""
        found: dict[str, Result] = {}
        for place in places:
            result = self.cached(place)
            if result is not None:
                found[place.key] = result
        return found

    def forget(self, key: str) -> None:
        if self.cache is not None:
            self.cache.remove(key)
