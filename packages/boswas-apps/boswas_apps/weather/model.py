"""The weather as Weather sees it (standard library only; no Qt).

Open-Meteo answers two questions:

* the place search (geocoding API): ``{"results": [{"id", "name", "latitude", "longitude", "country",
  "country_code", "admin1", "timezone", ...}]}``, without ``results`` when nothing matched;
* the forecast: ``{"utc_offset_seconds", "timezone", "current": {name: value}, "hourly": {name: [values]},
  "daily": {name: [values]}, "current_units", "hourly_units", "daily_units"}``. The arrays of a section are
  parallel to its ``time`` array; times are the place's local time without an offset ("2026-11-21T14:00").

Both answers come from the network and are treated as hostile: ``parse_places`` and ``parse_forecast``
take any JSON value and return dataclasses in which every field was checked (a number in a plausible
range or None, a short plain text, a valid time), or raise ``DataError``.

The forecast is always fetched in metric units; the functions at the end convert and format the values
for the units the user chose, so the cache does not depend on the settings.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from typing import Any

MAX_RESULTS = 20               # places taken from one search answer
MAX_HOURS = 16 * 24
MAX_DAYS = 16
MAX_NAME = 100
MAX_REGION = 100
MISSING = "—"                  # what is shown for a value the service did not give


class DataError(ValueError):
    """An answer of the weather service that cannot be used at all."""


# --- plain values from outside -----------------------------------------------------------------------

_BIDI = dict.fromkeys(map(ord, "؜‎‏‪‫‬‭‮⁦⁧⁨⁩"), " ")
_ZONE = re.compile(r"[A-Za-z0-9_+\-/]{1,64}")
_COUNTRY_CODE = re.compile(r"[A-Z]{2}")


def clean_text(value: Any, limit: int) -> str:
    """A short single-line text: control and direction-override characters removed, spaces collapsed."""
    if not isinstance(value, str):
        return ""
    value = value[:limit * 4].translate(_BIDI)
    value = "".join(" " if ord(character) < 32 or 127 <= ord(character) < 160 else character for character in value)
    return " ".join(value.split())[:limit].strip()


def place_text(value: Any, limit: int) -> str:
    """A name from the place search: ``clean_text`` without angle brackets, so that no tool tip or other
    text that Qt might take for rich text ever sees a tag in it."""
    return clean_text(value.replace("<", "").replace(">", "") if isinstance(value, str) else value, limit)


def number(value: Any, low: float, high: float) -> float | None:
    """A finite number within [low, high], or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int) and abs(value) > 10 ** 15:
        return None
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        return None
    return value


def weather_code(value: Any) -> int | None:
    """A WMO weather code (a whole number from 0 to 99), or None."""
    code = number(value, 0, 99)
    if code is None or code != int(code):
        return None
    return int(code)


def flag(value: Any) -> bool | None:
    """Open-Meteo's 0/1 flags (is_day) as a bool, or None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def parse_local_time(value: Any) -> dt.datetime | None:
    """A local time "YYYY-MM-DDTHH:MM" (seconds allowed) as a naive datetime, or None."""
    if not isinstance(value, str) or not 16 <= len(value) <= 19:
        return None
    for pattern in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            moment = dt.datetime.strptime(value, pattern)
        except ValueError:
            continue
        return moment if 1900 <= moment.year <= 2200 else None
    return None


def parse_date(value: Any) -> dt.date | None:
    """A date "YYYY-MM-DD", or None."""
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        day = dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    return day if 1900 <= day.year <= 2200 else None


# --- places ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Place:
    """A place of the place search, or one of the user's places."""
    name: str
    latitude: float
    longitude: float
    country: str = ""
    country_code: str = ""
    admin1: str = ""
    timezone: str = ""
    id: int | None = None

    @property
    def key(self) -> str:
        """What identifies the place in files: its Open-Meteo ID, else its rounded coordinates."""
        if self.id is not None:
            return str(self.id)
        return f"geo_{self.latitude:.4f}_{self.longitude:.4f}"

    def region(self) -> str:
        """The state and the country ("Land Berlin, Germany"), without repeating the name."""
        parts: list[str] = []
        for part in (self.admin1, self.country):
            if part and part != self.name and part not in parts:
                parts.append(part)
        return ", ".join(parts)

    def label(self) -> str:
        """The name with the region: "Berlin, Land Berlin, Germany"."""
        region = self.region()
        return f"{self.name}, {region}" if region else self.name

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "latitude": self.latitude, "longitude": self.longitude,
                "country": self.country, "country_code": self.country_code, "admin1": self.admin1,
                "timezone": self.timezone}

    @classmethod
    def from_json(cls, raw: Any) -> "Place | None":
        """A place from a search result or from ``places.json``; None when a required field is bad."""
        if not isinstance(raw, dict):
            return None
        name = place_text(raw.get("name"), MAX_NAME)
        latitude = number(raw.get("latitude"), -90, 90)
        longitude = number(raw.get("longitude"), -180, 180)
        if not name or latitude is None or longitude is None:
            return None
        identifier = raw.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int) or not 0 < identifier < 2 ** 53:
            identifier = None
        code = raw.get("country_code")
        code = code.upper() if isinstance(code, str) and _COUNTRY_CODE.fullmatch(code.upper()) else ""
        zone = raw.get("timezone")
        zone = zone if isinstance(zone, str) and _ZONE.fullmatch(zone) else ""
        return cls(name, round(latitude, 5), round(longitude, 5), place_text(raw.get("country"), MAX_REGION), code,
                   place_text(raw.get("admin1"), MAX_REGION), zone, identifier)


def parse_places(raw: Any) -> list[Place]:
    """The places of a search answer, in its order, each place once; [] when nothing matched."""
    if not isinstance(raw, dict):
        raise DataError("The answer to the search is not a JSON object.")
    if raw.get("error") is True:
        raise DataError(service_reason(raw) or "The search was refused.")
    results = raw.get("results")
    if results is None:
        return []
    if not isinstance(results, list):
        raise DataError("The answer to the search has no list of places.")
    places: list[Place] = []
    seen: set[str] = set()
    for entry in results[:MAX_RESULTS]:
        place = Place.from_json(entry)
        if place is not None and place.key not in seen:
            seen.add(place.key)
            places.append(place)
    return places


def service_reason(raw: Any) -> str:
    """The ``reason`` of an Open-Meteo error answer, as a short plain text ("" when there is none)."""
    return clean_text(raw.get("reason"), 200) if isinstance(raw, dict) else ""


# --- the forecast ------------------------------------------------------------------------------------

TEMPERATURE = (-100.0, 70.0)
PERCENT = (0.0, 100.0)
PRECIPITATION = (0.0, 2000.0)
PRESSURE = (800.0, 1200.0)
WIND = (0.0, 600.0)
DIRECTION = (0.0, 360.0)
VISIBILITY = (0.0, 1_000_000.0)
UV = (0.0, 40.0)

# The units Open-Meteo gives for metric answers. A field whose unit is said to be something else is ignored.
EXPECTED_UNITS = {
    "temperature_2m": "°C", "apparent_temperature": "°C", "temperature_2m_max": "°C", "temperature_2m_min": "°C",
    "apparent_temperature_max": "°C", "apparent_temperature_min": "°C", "precipitation": "mm",
    "precipitation_sum": "mm", "wind_speed_10m": "km/h", "wind_gusts_10m": "km/h", "wind_speed_10m_max": "km/h",
    "pressure_msl": "hPa", "visibility": "m",
}


@dataclass(frozen=True)
class Current:
    """The conditions now (or at ``time``)."""
    time: dt.datetime | None = None
    temperature: float | None = None
    humidity: float | None = None
    apparent: float | None = None
    is_day: bool | None = None
    precipitation: float | None = None
    code: int | None = None
    cloud_cover: float | None = None
    pressure: float | None = None
    wind_speed: float | None = None
    wind_direction: float | None = None
    wind_gusts: float | None = None
    visibility: float | None = None
    uv_index: float | None = None


@dataclass(frozen=True)
class Hour:
    time: dt.datetime
    temperature: float | None = None
    apparent: float | None = None
    precipitation_probability: float | None = None
    precipitation: float | None = None
    code: int | None = None
    is_day: bool | None = None
    wind_speed: float | None = None


@dataclass(frozen=True)
class Day:
    date: dt.date
    code: int | None = None
    temperature_max: float | None = None
    temperature_min: float | None = None
    apparent_max: float | None = None
    apparent_min: float | None = None
    sunrise: dt.datetime | None = None
    sunset: dt.datetime | None = None
    uv_index_max: float | None = None
    precipitation_sum: float | None = None
    precipitation_probability_max: float | None = None
    wind_speed_max: float | None = None
    wind_direction_dominant: float | None = None


@dataclass(frozen=True)
class Forecast:
    timezone: str
    utc_offset: int                    # seconds east of UTC
    current: Current | None
    hours: tuple[Hour, ...]
    days: tuple[Day, ...]

    def local_now(self, epoch: float) -> dt.datetime:
        """The place's local time (naive, like the times of the forecast) at ``epoch``."""
        return dt.datetime(1970, 1, 1) + dt.timedelta(seconds=epoch + self.utc_offset)

    def hours_from(self, moment: dt.datetime, count: int = 24) -> list[Hour]:
        """``count`` hours from the hour that contains ``moment`` on."""
        start = moment.replace(minute=0, second=0, microsecond=0)
        return [hour for hour in self.hours if hour.time >= start][:count]

    def days_from(self, day: dt.date, count: int = 10) -> list[Day]:
        return [entry for entry in self.days if entry.date >= day][:count]

    def day(self, day: dt.date) -> Day | None:
        return next((entry for entry in self.days if entry.date == day), None)

    def conditions(self, moment: dt.datetime) -> Current | None:
        """The current conditions of the answer, else what the hourly forecast says for ``moment``."""
        if self.current is not None:
            return self.current
        hours = self.hours_from(moment, 1)
        if not hours:
            return None
        hour = hours[0]
        return Current(time=hour.time, temperature=hour.temperature, apparent=hour.apparent, is_day=hour.is_day,
                       precipitation=hour.precipitation, code=hour.code, wind_speed=hour.wind_speed)


def _unit_ok(units: Any, name: str) -> bool:
    expected = EXPECTED_UNITS.get(name)
    if expected is None or not isinstance(units, dict) or name not in units:
        return True
    return units[name] == expected


def _value(section: dict, units: Any, name: str, limits: tuple[float, float]) -> float | None:
    if not _unit_ok(units, name):
        return None
    return number(section.get(name), *limits)


def _series(section: dict, units: Any, name: str, length: int) -> list:
    values = section.get(name)
    if not isinstance(values, list) or not _unit_ok(units, name):
        return [None] * length
    values = values[:length]
    return values + [None] * (length - len(values))


def _parse_current(section: Any, units: Any) -> Current | None:
    if not isinstance(section, dict):
        return None
    current = Current(
        time=parse_local_time(section.get("time")),
        temperature=_value(section, units, "temperature_2m", TEMPERATURE),
        humidity=_value(section, units, "relative_humidity_2m", PERCENT),
        apparent=_value(section, units, "apparent_temperature", TEMPERATURE),
        is_day=flag(section.get("is_day")),
        precipitation=_value(section, units, "precipitation", PRECIPITATION),
        code=weather_code(section.get("weather_code")),
        cloud_cover=_value(section, units, "cloud_cover", PERCENT),
        pressure=_value(section, units, "pressure_msl", PRESSURE),
        wind_speed=_value(section, units, "wind_speed_10m", WIND),
        wind_direction=_value(section, units, "wind_direction_10m", DIRECTION),
        wind_gusts=_value(section, units, "wind_gusts_10m", WIND),
        visibility=_value(section, units, "visibility", VISIBILITY),
        uv_index=_value(section, units, "uv_index", UV),
    )
    if current.temperature is None and current.code is None:
        return None                     # nothing worth showing as "now"
    return current


def _parse_hours(section: Any, units: Any) -> tuple[Hour, ...]:
    if not isinstance(section, dict) or not isinstance(section.get("time"), list):
        return ()
    times = section["time"][:MAX_HOURS]
    length = len(times)
    columns = {name: _series(section, units, name, length) for name in (
        "temperature_2m", "apparent_temperature", "precipitation_probability", "precipitation", "weather_code",
        "is_day", "wind_speed_10m")}
    hours: list[Hour] = []
    for index, raw_time in enumerate(times):
        moment = parse_local_time(raw_time)
        if moment is None or (hours and moment <= hours[-1].time):
            continue                    # a bad or out-of-order time: the hour is dropped
        hours.append(Hour(
            time=moment,
            temperature=number(columns["temperature_2m"][index], *TEMPERATURE),
            apparent=number(columns["apparent_temperature"][index], *TEMPERATURE),
            precipitation_probability=number(columns["precipitation_probability"][index], *PERCENT),
            precipitation=number(columns["precipitation"][index], *PRECIPITATION),
            code=weather_code(columns["weather_code"][index]),
            is_day=flag(columns["is_day"][index]),
            wind_speed=number(columns["wind_speed_10m"][index], *WIND)))
    return tuple(hours)


def _parse_days(section: Any, units: Any) -> tuple[Day, ...]:
    if not isinstance(section, dict) or not isinstance(section.get("time"), list):
        return ()
    dates = section["time"][:MAX_DAYS]
    length = len(dates)
    columns = {name: _series(section, units, name, length) for name in (
        "weather_code", "temperature_2m_max", "temperature_2m_min", "apparent_temperature_max",
        "apparent_temperature_min", "sunrise", "sunset", "uv_index_max", "precipitation_sum",
        "precipitation_probability_max", "wind_speed_10m_max", "wind_direction_10m_dominant")}
    days: list[Day] = []
    for index, raw_date in enumerate(dates):
        day = parse_date(raw_date)
        if day is None or (days and day <= days[-1].date):
            continue
        high = number(columns["temperature_2m_max"][index], *TEMPERATURE)
        low = number(columns["temperature_2m_min"][index], *TEMPERATURE)
        if high is not None and low is not None and low > high:
            low, high = high, low
        sunrise = parse_local_time(columns["sunrise"][index])
        sunset = parse_local_time(columns["sunset"][index])
        days.append(Day(
            date=day,
            code=weather_code(columns["weather_code"][index]),
            temperature_max=high,
            temperature_min=low,
            apparent_max=number(columns["apparent_temperature_max"][index], *TEMPERATURE),
            apparent_min=number(columns["apparent_temperature_min"][index], *TEMPERATURE),
            sunrise=sunrise if sunrise is not None and sunrise.date() == day else None,
            sunset=sunset if sunset is not None and sunset.date() == day else None,
            uv_index_max=number(columns["uv_index_max"][index], *UV),
            precipitation_sum=number(columns["precipitation_sum"][index], *PRECIPITATION),
            precipitation_probability_max=number(columns["precipitation_probability_max"][index], *PERCENT),
            wind_speed_max=number(columns["wind_speed_10m_max"][index], *WIND),
            wind_direction_dominant=number(columns["wind_direction_10m_dominant"][index], *DIRECTION)))
    return tuple(days)


def parse_forecast(raw: Any) -> Forecast:
    """The forecast answer as a ``Forecast``; raises DataError when it holds nothing usable."""
    if not isinstance(raw, dict):
        raise DataError("The forecast is not a JSON object.")
    if raw.get("error") is True:
        raise DataError(service_reason(raw) or "The forecast was refused.")
    offset = number(raw.get("utc_offset_seconds"), -18 * 3600, 18 * 3600)
    zone = raw.get("timezone")
    forecast = Forecast(
        timezone=zone if isinstance(zone, str) and _ZONE.fullmatch(zone) else "",
        utc_offset=int(offset) if offset is not None else 0,
        current=_parse_current(raw.get("current"), raw.get("current_units")),
        hours=_parse_hours(raw.get("hourly"), raw.get("hourly_units")),
        days=_parse_days(raw.get("daily"), raw.get("daily_units")))
    if forecast.current is None and not forecast.hours and not forecast.days:
        raise DataError("The forecast holds no weather.")
    return forecast


# --- weather codes -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Condition:
    day_text: str
    night_text: str
    day_icon: str
    night_icon: str


def _same(text: str, icon: str) -> Condition:
    return Condition(text, text, icon, icon)


CLEAR = Condition("Sunny", "Clear", "weather-clear-symbolic", "weather-clear-night-symbolic")
UNKNOWN = _same("Unknown conditions", "weather-clouds-symbolic")

# Every code of the WMO table that Open-Meteo uses (WMO 4677, as documented by Open-Meteo).
WMO: dict[int, Condition] = {
    0: CLEAR,
    1: Condition("Mostly sunny", "Mostly clear", "weather-few-clouds-symbolic", "weather-few-clouds-night-symbolic"),
    2: Condition("Partly cloudy", "Partly cloudy", "weather-few-clouds-symbolic",
                 "weather-few-clouds-night-symbolic"),
    3: _same("Overcast", "weather-overcast-symbolic"),
    45: _same("Fog", "weather-fog-symbolic"),
    48: _same("Freezing fog", "weather-fog-symbolic"),
    51: _same("Light drizzle", "weather-showers-scattered-symbolic"),
    53: _same("Drizzle", "weather-showers-scattered-symbolic"),
    55: _same("Heavy drizzle", "weather-showers-scattered-symbolic"),
    56: _same("Light freezing drizzle", "weather-showers-scattered-symbolic"),
    57: _same("Freezing drizzle", "weather-showers-scattered-symbolic"),
    61: _same("Light rain", "weather-showers-symbolic"),
    63: _same("Rain", "weather-showers-symbolic"),
    65: _same("Heavy rain", "weather-showers-symbolic"),
    66: _same("Light freezing rain", "weather-showers-symbolic"),
    67: _same("Freezing rain", "weather-showers-symbolic"),
    71: _same("Light snow", "weather-snow-symbolic"),
    73: _same("Snow", "weather-snow-symbolic"),
    75: _same("Heavy snow", "weather-snow-symbolic"),
    77: _same("Snow grains", "weather-snow-symbolic"),
    80: _same("Light showers", "weather-showers-scattered-symbolic"),
    81: _same("Showers", "weather-showers-symbolic"),
    82: _same("Heavy showers", "weather-showers-symbolic"),
    85: _same("Snow showers", "weather-snow-symbolic"),
    86: _same("Heavy snow showers", "weather-snow-symbolic"),
    95: _same("Thunderstorm", "weather-storm-symbolic"),
    96: _same("Thunderstorm with hail", "weather-storm-symbolic"),
    99: _same("Thunderstorm with heavy hail", "weather-storm-symbolic"),
}

CONDITION_ICONS = frozenset(icon for condition in (*WMO.values(), UNKNOWN)
                            for icon in (condition.day_icon, condition.night_icon))


def condition_text(code: int | None, is_day: bool | None = True) -> str:
    """The words for a weather code ("Light rain"); night words for a clear sky at night."""
    condition = WMO.get(code, UNKNOWN) if code is not None else UNKNOWN
    return condition.night_text if is_day is False else condition.day_text


def condition_icon(code: int | None, is_day: bool | None = True) -> str:
    """The symbolic icon name for a weather code, with the night variant when ``is_day`` is False."""
    condition = WMO.get(code, UNKNOWN) if code is not None else UNKNOWN
    return condition.night_icon if is_day is False else condition.day_icon


# --- units -------------------------------------------------------------------------------------------

TEMPERATURE_UNITS = {"c": "°C", "f": "°F"}
WIND_UNITS = {"kmh": "km/h", "mph": "mph", "ms": "m/s", "kn": "kn"}
PRECIPITATION_UNITS = {"mm": "mm", "in": "in"}
PRESSURE_UNITS = {"hpa": "hPa", "inhg": "inHg"}
DISTANCE_UNITS = {"km": "km", "mi": "mi"}


def convert_temperature(celsius: float, unit: str) -> float:
    return celsius * 9 / 5 + 32 if unit == "f" else celsius


def convert_wind(kmh: float, unit: str) -> float:
    return {"mph": kmh / 1.609344, "ms": kmh / 3.6, "kn": kmh / 1.852}.get(unit, kmh)


def convert_precipitation(mm: float, unit: str) -> float:
    return mm / 25.4 if unit == "in" else mm


def convert_pressure(hpa: float, unit: str) -> float:
    return hpa * 0.029529983071445 if unit == "inhg" else hpa


def convert_distance(metres: float, unit: str) -> float:
    return metres / 1609.344 if unit == "mi" else metres / 1000


def distance_unit(wind_unit: str) -> str:
    """Visibility is given in miles with wind in miles per hour, else in kilometres."""
    return "mi" if wind_unit == "mph" else "km"


def whole(value: float) -> int:
    """Rounded half away from zero (12.5 is 13, -2.5 is -3), as people round."""
    return int(math.floor(abs(value) + 0.5)) * (-1 if value < 0 else 1)


# --- formatting --------------------------------------------------------------------------------------

def format_temperature(celsius: float | None, unit: str, *, with_unit: bool = False) -> str:
    """ "12°" (or "12°C" with the unit); MISSING without a value."""
    if celsius is None:
        return MISSING
    value = whole(convert_temperature(celsius, unit))
    return f"{value}{TEMPERATURE_UNITS.get(unit, '°C')}" if with_unit else f"{value}°"


def format_wind(kmh: float | None, unit: str) -> str:
    if kmh is None:
        return MISSING
    value = convert_wind(kmh, unit)
    if unit == "ms":
        return f"{value:.1f} m/s"
    return f"{whole(value)} {WIND_UNITS.get(unit, 'km/h')}"


def format_precipitation(mm: float | None, unit: str) -> str:
    if mm is None:
        return MISSING
    value = convert_precipitation(mm, unit)
    return f"{value:.2f} in" if unit == "in" else f"{value:.1f} mm"


def format_pressure(hpa: float | None, unit: str) -> str:
    if hpa is None:
        return MISSING
    value = convert_pressure(hpa, unit)
    return f"{value:.2f} inHg" if unit == "inhg" else f"{whole(value)} hPa"


def format_visibility(metres: float | None, unit: str) -> str:
    if metres is None:
        return MISSING
    value = convert_distance(metres, unit)
    text = f"{whole(value)}" if value >= 10 else f"{value:.1f}"
    return f"{text} {DISTANCE_UNITS.get(unit, 'km')}"


def format_percent(value: float | None) -> str:
    return MISSING if value is None else f"{whole(value)}%"


def format_uv(value: float | None) -> str:
    return MISSING if value is None else str(whole(value))


def uv_category(value: float | None) -> str:
    """The WHO category of a UV index."""
    if value is None:
        return ""
    index = whole(value)
    if index <= 2:
        return "Low"
    if index <= 5:
        return "Moderate"
    if index <= 7:
        return "High"
    if index <= 10:
        return "Very high"
    return "Extreme"


COMPASS_POINTS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
COMPASS_NAMES = ("north", "north-northeast", "northeast", "east-northeast", "east", "east-southeast", "southeast",
                 "south-southeast", "south", "south-southwest", "southwest", "west-southwest", "west",
                 "west-northwest", "northwest", "north-northwest")


def _compass_index(degrees: float) -> int:
    return int((degrees % 360) / 22.5 + 0.5) % 16


def compass_point(degrees: float | None) -> str:
    """The point of the 16-point compass ("SW") the wind comes from."""
    return "" if degrees is None else COMPASS_POINTS[_compass_index(degrees)]


def compass_name(degrees: float | None) -> str:
    """The same in words ("southwest")."""
    return "" if degrees is None else COMPASS_NAMES[_compass_index(degrees)]


BEAUFORT = ((1, "Calm"), (6, "Light air"), (12, "Light breeze"), (20, "Gentle breeze"), (29, "Moderate breeze"),
            (39, "Fresh breeze"), (50, "Strong breeze"), (62, "Near gale"), (75, "Gale"), (89, "Strong gale"),
            (103, "Storm"), (118, "Violent storm"))


def beaufort(kmh: float | None) -> str:
    """The name of the wind force on the Beaufort scale ("Gentle breeze")."""
    if kmh is None:
        return ""
    for limit, name in BEAUFORT:
        if kmh < limit:
            return name
    return "Hurricane force"


def dew_point(celsius: float | None, humidity: float | None) -> float | None:
    """The dew point (Magnus formula) from temperature and relative humidity."""
    if celsius is None or humidity is None or humidity <= 0:
        return None
    a, b = 17.62, 243.12
    gamma = math.log(humidity / 100) + a * celsius / (b + celsius)
    return b * gamma / (a - gamma)


def visibility_words(metres: float | None) -> str:
    if metres is None:
        return ""
    if metres >= 10_000:
        return "Good visibility"
    if metres >= 4_000:
        return "Moderate visibility"
    if metres >= 1_000:
        return "Poor visibility"
    return "Very poor visibility"


def cloud_words(percent: float | None) -> str:
    if percent is None:
        return ""
    if percent <= 10:
        return "Clear sky"
    if percent <= 35:
        return "A few clouds"
    if percent <= 65:
        return "Partly cloudy"
    if percent <= 90:
        return "Mostly cloudy"
    return "Overcast"


# --- times -------------------------------------------------------------------------------------------

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEEKDAYS_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_clock(moment: dt.datetime | dt.time | None, twelve_hour: bool) -> str:
    """ "14:05", or "2:05 PM" with the 12-hour clock."""
    if moment is None:
        return MISSING
    if twelve_hour:
        return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"
    return f"{moment.hour:02d}:{moment.minute:02d}"


def format_hour(moment: dt.datetime | dt.time, twelve_hour: bool) -> str:
    """A full hour: "14:00", or "2 PM"."""
    if twelve_hour:
        return f"{moment.hour % 12 or 12} {'AM' if moment.hour < 12 else 'PM'}"
    return f"{moment.hour:02d}:00"


def format_date(day: dt.date) -> str:
    """ "Sat 21 Nov"."""
    return f"{WEEKDAYS_SHORT[day.weekday()]} {day.day} {MONTHS_SHORT[day.month - 1]}"


def day_label(day: dt.date, today: dt.date) -> str:
    """The short label of a row of the 10-day forecast: "Today", "Sun 22"."""
    if day == today:
        return "Today"
    return f"{WEEKDAYS_SHORT[day.weekday()]} {day.day}"


def day_name(day: dt.date, today: dt.date) -> str:
    """The day in words: "Today", "Tomorrow", "Monday 23 November"."""
    if day == today:
        return "Today"
    if day == today + dt.timedelta(days=1):
        return "Tomorrow"
    return f"{WEEKDAYS[day.weekday()]} {day.day} {MONTHS_SHORT[day.month - 1]}"


def format_duration(seconds: float) -> str:
    """ "8 h 27 min"."""
    minutes = max(0, int(seconds // 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} h {minutes} min"
    return f"{hours} h" if hours else f"{minutes} min"


def updated_text(fetched: float, now: float, twelve_hour: bool, zone: dt.tzinfo | None = None) -> str:
    """When a forecast was fetched, in the user's own time: "Updated at 14:05", "Updated yesterday at 9:10"."""
    when = dt.datetime.fromtimestamp(fetched, zone) if zone is not None else dt.datetime.fromtimestamp(fetched)
    today = (dt.datetime.fromtimestamp(now, zone) if zone is not None else dt.datetime.fromtimestamp(now)).date()
    clock = format_clock(when, twelve_hour)
    if when.date() >= today:
        return f"Updated at {clock}"
    if when.date() == today - dt.timedelta(days=1):
        return f"Updated yesterday at {clock}"
    return f"Updated {format_date(when.date())} at {clock}"


# --- what the 10-day list paints ---------------------------------------------------------------------

def range_span(low: float | None, high: float | None, lowest: float | None,
               highest: float | None) -> tuple[float, float] | None:
    """Where a day's low and high lie (0 to 1) on the scale from the lowest to the highest of all days."""
    if low is None or high is None or lowest is None or highest is None:
        return None
    if highest - lowest < 1e-9:
        return 0.0, 1.0
    start = (low - lowest) / (highest - lowest)
    end = (high - lowest) / (highest - lowest)
    return max(0.0, min(1.0, start)), max(0.0, min(1.0, end))


def describe_hour(hour: Hour, unit: str, twelve_hour: bool, *, now: bool = False) -> str:
    """One hour in words, for screen readers."""
    parts = ["Now" if now else format_hour(hour.time, twelve_hour), condition_text(hour.code, hour.is_day),
             format_temperature(hour.temperature, unit, with_unit=True)]
    if hour.precipitation_probability is not None:
        parts.append(f"{format_percent(hour.precipitation_probability)} chance of rain")
    return ", ".join(parts)


def describe_day(day: Day, today: dt.date, unit: str) -> str:
    """One day in words, for screen readers."""
    parts = [day_name(day.date, today), condition_text(day.code),
             f"high {format_temperature(day.temperature_max, unit, with_unit=True)}",
             f"low {format_temperature(day.temperature_min, unit, with_unit=True)}"]
    if day.precipitation_probability_max is not None:
        parts.append(f"{format_percent(day.precipitation_probability_max)} chance of rain")
    return ", ".join(parts)
