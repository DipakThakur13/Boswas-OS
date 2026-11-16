"""Reading and writing iCalendar files (RFC 5545), standard library only.

Reading (``parse``) takes the bytes of a ``.ics`` file and returns the
events it holds as ``model.Event`` values without a calendar, plus what
was skipped. The file is treated as hostile: its size, the length of a
line, the number of events and the nesting of components are capped;
nothing in it is executed or followed; recurrence rules are only parsed
here, and expanded later within the bounds of ``recurrence``.

What is read: VEVENT with DTSTART, DTEND or DURATION (dates for all-day
events, local "floating" times, UTC times, times with TZID), SUMMARY,
LOCATION, DESCRIPTION, RRULE, RDATE, EXDATE, RECURRENCE-ID, SEQUENCE,
CREATED, LAST-MODIFIED and VALARM (a trigger relative to the start or the
end becomes a reminder; an absolute trigger does when the event does not
repeat). Other properties of an event are kept as they were and written
back on export. VTODO, VJOURNAL and VFREEBUSY are counted and skipped.
A TZID is mapped to a zone of the system's time-zone database: by its name,
by a name hidden in it (``/mozilla.org/.../Europe/Berlin``), by its Windows
name (``W. Europe Standard Time``), or by comparing the offsets of its
VTIMEZONE with the database. When nothing fits, the times are converted
with the VTIMEZONE's own offsets and stored in UTC.
RECURRENCE-ID with RANGE=THISANDFUTURE is read as a change of that one
occurrence.

Writing (``export``) produces CRLF lines folded at 75 octets, escaped
texts, the UID each event has always had, and a VTIMEZONE for every zone
used. A VTIMEZONE is written from the rules of the time-zone database:
the transitions of the years the events cover, one by one, and the yearly
rule (``last Sunday of March``) from the first year it holds.
"""

from __future__ import annotations

import calendar as _calendar
import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field, replace
from typing import Iterable, Iterator

from ..reminders import timeutil
from . import model, recurrence

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_LINE = 64 * 1024               # octets of one unfolded line; the rest of a longer line is dropped
MAX_EVENTS = 50_000
MAX_DEPTH = 8
MAX_PARAMS = 32
MAX_VTIMEZONES = 256
PRODID = "-//Boswas OS//Boswas Calendar 1.0//EN"
UTC = timeutil.UTC

_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DATETIME = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$")
_DURATION = re.compile(r"^([+-]?)P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")
_OFFSET = re.compile(r"^([+-])(\d{2})(\d{2})(\d{2})?$")
_GMT = re.compile(r"^(?:ETC/)?(?:GMT|UTC)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$")
_KNOWN = {"UID", "DTSTART", "DTEND", "DURATION", "SUMMARY", "LOCATION", "DESCRIPTION", "RRULE", "RDATE", "EXDATE",
          "RECURRENCE-ID", "SEQUENCE", "CREATED", "LAST-MODIFIED", "DTSTAMP"}

# Windows time-zone names (as Outlook and Exchange write them into TZID) -> zones of the database.
# Where a zone was renamed, both names are listed and the first one the system has is used.
WINDOWS_ZONES: dict[str, tuple[str, ...]] = {
    "Dateline Standard Time": ("Etc/GMT+12",), "UTC-11": ("Etc/GMT+11",),
    "Aleutian Standard Time": ("America/Adak",), "Hawaiian Standard Time": ("Pacific/Honolulu",),
    "Marquesas Standard Time": ("Pacific/Marquesas",), "Alaskan Standard Time": ("America/Anchorage",),
    "UTC-09": ("Etc/GMT+9",), "Pacific Standard Time (Mexico)": ("America/Tijuana",), "UTC-08": ("Etc/GMT+8",),
    "Pacific Standard Time": ("America/Los_Angeles",), "US Mountain Standard Time": ("America/Phoenix",),
    "Mountain Standard Time (Mexico)": ("America/Mazatlan",), "Mountain Standard Time": ("America/Denver",),
    "Yukon Standard Time": ("America/Whitehorse",), "Central America Standard Time": ("America/Guatemala",),
    "Central Standard Time": ("America/Chicago",), "Easter Island Standard Time": ("Pacific/Easter",),
    "Central Standard Time (Mexico)": ("America/Mexico_City",),
    "Canada Central Standard Time": ("America/Regina",), "SA Pacific Standard Time": ("America/Bogota",),
    "Eastern Standard Time (Mexico)": ("America/Cancun",), "Eastern Standard Time": ("America/New_York",),
    "Haiti Standard Time": ("America/Port-au-Prince",), "Cuba Standard Time": ("America/Havana",),
    "US Eastern Standard Time": ("America/Indiana/Indianapolis",),
    "Turks And Caicos Standard Time": ("America/Grand_Turk",), "Paraguay Standard Time": ("America/Asuncion",),
    "Atlantic Standard Time": ("America/Halifax",), "Venezuela Standard Time": ("America/Caracas",),
    "Central Brazilian Standard Time": ("America/Cuiaba",), "SA Western Standard Time": ("America/La_Paz",),
    "Pacific SA Standard Time": ("America/Santiago",), "Newfoundland Standard Time": ("America/St_Johns",),
    "Tocantins Standard Time": ("America/Araguaina",), "E. South America Standard Time": ("America/Sao_Paulo",),
    "SA Eastern Standard Time": ("America/Cayenne",),
    "Argentina Standard Time": ("America/Argentina/Buenos_Aires",),
    "Greenland Standard Time": ("America/Nuuk", "America/Godthab"),
    "Montevideo Standard Time": ("America/Montevideo",), "Magallanes Standard Time": ("America/Punta_Arenas",),
    "Saint Pierre Standard Time": ("America/Miquelon",), "Bahia Standard Time": ("America/Bahia",),
    "UTC-02": ("Etc/GMT+2",), "Azores Standard Time": ("Atlantic/Azores",),
    "Cape Verde Standard Time": ("Atlantic/Cape_Verde",), "UTC": ("Etc/UTC",),
    "GMT Standard Time": ("Europe/London",), "Greenwich Standard Time": ("Atlantic/Reykjavik",),
    "Sao Tome Standard Time": ("Africa/Sao_Tome",), "Morocco Standard Time": ("Africa/Casablanca",),
    "W. Europe Standard Time": ("Europe/Berlin",), "Central Europe Standard Time": ("Europe/Budapest",),
    "Romance Standard Time": ("Europe/Paris",), "Central European Standard Time": ("Europe/Warsaw",),
    "W. Central Africa Standard Time": ("Africa/Lagos",), "Jordan Standard Time": ("Asia/Amman",),
    "GTB Standard Time": ("Europe/Bucharest",), "Middle East Standard Time": ("Asia/Beirut",),
    "Egypt Standard Time": ("Africa/Cairo",), "E. Europe Standard Time": ("Europe/Chisinau",),
    "Syria Standard Time": ("Asia/Damascus",), "West Bank Standard Time": ("Asia/Hebron",),
    "South Africa Standard Time": ("Africa/Johannesburg",), "FLE Standard Time": ("Europe/Kyiv", "Europe/Kiev"),
    "Israel Standard Time": ("Asia/Jerusalem",), "South Sudan Standard Time": ("Africa/Juba",),
    "Kaliningrad Standard Time": ("Europe/Kaliningrad",), "Sudan Standard Time": ("Africa/Khartoum",),
    "Libya Standard Time": ("Africa/Tripoli",), "Namibia Standard Time": ("Africa/Windhoek",),
    "Arabic Standard Time": ("Asia/Baghdad",), "Turkey Standard Time": ("Europe/Istanbul",),
    "Arab Standard Time": ("Asia/Riyadh",), "Belarus Standard Time": ("Europe/Minsk",),
    "Russian Standard Time": ("Europe/Moscow",), "E. Africa Standard Time": ("Africa/Nairobi",),
    "Volgograd Standard Time": ("Europe/Volgograd",), "Iran Standard Time": ("Asia/Tehran",),
    "Arabian Standard Time": ("Asia/Dubai",), "Astrakhan Standard Time": ("Europe/Astrakhan",),
    "Azerbaijan Standard Time": ("Asia/Baku",), "Russia Time Zone 3": ("Europe/Samara",),
    "Mauritius Standard Time": ("Indian/Mauritius",), "Saratov Standard Time": ("Europe/Saratov",),
    "Georgian Standard Time": ("Asia/Tbilisi",), "Caucasus Standard Time": ("Asia/Yerevan",),
    "Afghanistan Standard Time": ("Asia/Kabul",), "West Asia Standard Time": ("Asia/Tashkent",),
    "Ekaterinburg Standard Time": ("Asia/Yekaterinburg",), "Pakistan Standard Time": ("Asia/Karachi",),
    "Qyzylorda Standard Time": ("Asia/Qyzylorda",), "India Standard Time": ("Asia/Kolkata", "Asia/Calcutta"),
    "Sri Lanka Standard Time": ("Asia/Colombo",), "Nepal Standard Time": ("Asia/Kathmandu", "Asia/Katmandu"),
    "Central Asia Standard Time": ("Asia/Almaty",), "Bangladesh Standard Time": ("Asia/Dhaka",),
    "Omsk Standard Time": ("Asia/Omsk",), "Myanmar Standard Time": ("Asia/Yangon", "Asia/Rangoon"),
    "SE Asia Standard Time": ("Asia/Bangkok",), "Altai Standard Time": ("Asia/Barnaul",),
    "W. Mongolia Standard Time": ("Asia/Hovd",), "North Asia Standard Time": ("Asia/Krasnoyarsk",),
    "N. Central Asia Standard Time": ("Asia/Novosibirsk",), "Tomsk Standard Time": ("Asia/Tomsk",),
    "China Standard Time": ("Asia/Shanghai",), "North Asia East Standard Time": ("Asia/Irkutsk",),
    "Singapore Standard Time": ("Asia/Singapore",), "W. Australia Standard Time": ("Australia/Perth",),
    "Taipei Standard Time": ("Asia/Taipei",), "Ulaanbaatar Standard Time": ("Asia/Ulaanbaatar",),
    "Aus Central W. Standard Time": ("Australia/Eucla",), "Transbaikal Standard Time": ("Asia/Chita",),
    "Tokyo Standard Time": ("Asia/Tokyo",), "North Korea Standard Time": ("Asia/Pyongyang",),
    "Korea Standard Time": ("Asia/Seoul",), "Yakutsk Standard Time": ("Asia/Yakutsk",),
    "Cen. Australia Standard Time": ("Australia/Adelaide",), "AUS Central Standard Time": ("Australia/Darwin",),
    "E. Australia Standard Time": ("Australia/Brisbane",), "AUS Eastern Standard Time": ("Australia/Sydney",),
    "West Pacific Standard Time": ("Pacific/Port_Moresby",), "Tasmania Standard Time": ("Australia/Hobart",),
    "Vladivostok Standard Time": ("Asia/Vladivostok",), "Lord Howe Standard Time": ("Australia/Lord_Howe",),
    "Bougainville Standard Time": ("Pacific/Bougainville",), "Russia Time Zone 10": ("Asia/Srednekolymsk",),
    "Magadan Standard Time": ("Asia/Magadan",), "Norfolk Standard Time": ("Pacific/Norfolk",),
    "Sakhalin Standard Time": ("Asia/Sakhalin",), "Central Pacific Standard Time": ("Pacific/Guadalcanal",),
    "Russia Time Zone 11": ("Asia/Kamchatka",), "New Zealand Standard Time": ("Pacific/Auckland",),
    "UTC+12": ("Etc/GMT-12",), "Fiji Standard Time": ("Pacific/Fiji",),
    "Chatham Islands Standard Time": ("Pacific/Chatham",), "UTC+13": ("Etc/GMT-13",),
    "Tonga Standard Time": ("Pacific/Tongatapu",), "Samoa Standard Time": ("Pacific/Apia",),
    "Line Islands Standard Time": ("Pacific/Kiritimati",),
}
# Zones tried first when a VTIMEZONE is matched by its offsets: where most people live.
_COMMON_ZONES = ("Europe/London", "Europe/Berlin", "Europe/Paris", "Europe/Madrid", "Europe/Rome", "Europe/Athens",
                 "Europe/Helsinki", "Europe/Moscow", "Europe/Istanbul", "America/New_York", "America/Chicago",
                 "America/Denver", "America/Phoenix", "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu",
                 "America/Toronto", "America/Mexico_City", "America/Bogota", "America/Sao_Paulo",
                 "America/Argentina/Buenos_Aires", "America/Santiago", "Africa/Cairo", "Africa/Lagos",
                 "Africa/Johannesburg", "Africa/Nairobi", "Asia/Dubai", "Asia/Tehran", "Asia/Karachi", "Asia/Kolkata",
                 "Asia/Dhaka", "Asia/Bangkok", "Asia/Shanghai", "Asia/Singapore", "Asia/Tokyo", "Asia/Seoul",
                 "Australia/Perth", "Australia/Adelaide", "Australia/Brisbane", "Australia/Sydney",
                 "Pacific/Auckland", "UTC")


class ICalError(ValueError):
    """The data is not an iCalendar file this program can read; the message is fit for the user."""


# --- content lines ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Line:
    name: str                          # upper case
    params: dict[str, list[str]]       # names in upper case
    value: str
    raw: str                           # the unfolded line as it was

    def param(self, name: str) -> str:
        values = self.params.get(name)
        return values[0] if values else ""


def decode(data: bytes) -> str:
    """The text of a file: UTF-8 (with or without a byte order mark), else Latin-1."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def unfold(text: str) -> Iterator[str]:
    """The logical lines: a line that starts with a space or a tab continues the one before."""
    current: list[str] | None = None
    size = 0
    for physical in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if physical[:1] in (" ", "\t") and current is not None:
            if size < MAX_LINE:
                current.append(physical[1:MAX_LINE - size + 1])
                size += len(physical) - 1
            continue
        if current is not None:
            yield "".join(current)
        current = [physical[:MAX_LINE]]
        size = len(physical)
    if current is not None:
        yield "".join(current)


def _caret(value: str) -> str:
    """RFC 6868: ^n is a line break, ^' a double quote, ^^ a caret, inside parameter values."""
    if "^" not in value:
        return value
    return re.sub(r"\^([n'^])", lambda match: {"n": "\n", "'": '"', "^": "^"}[match.group(1)], value)


def parse_line(raw: str) -> Line | None:
    """One content line (``NAME;PARAM=value:VALUE``), or None when it is not one."""
    position = 0
    length = len(raw)
    while position < length and raw[position] not in ";:":
        position += 1
    name = raw[:position].strip()
    if "." in name:                                   # a group prefix (vCard style): "item1.SUMMARY"
        name = name.rsplit(".", 1)[1]
    if position >= length or not _NAME.match(name):
        return None
    params: dict[str, list[str]] = {}
    while position < length and raw[position] == ";":
        position += 1
        begin = position
        while position < length and raw[position] not in "=;:":
            position += 1
        param = raw[begin:position].strip().upper()
        values: list[str] = []
        if position < length and raw[position] == "=":
            while True:
                position += 1
                if position < length and raw[position] == '"':
                    end = raw.find('"', position + 1)
                    if end < 0:
                        end = length
                    values.append(_caret(raw[position + 1:end]))
                    position = min(length, end + 1)
                else:
                    begin = position
                    while position < length and raw[position] not in ",;:":
                        position += 1
                    values.append(_caret(raw[begin:position]))
                if position >= length or raw[position] != ",":
                    break
        if param and _NAME.match(param) and len(params) < MAX_PARAMS:
            params[param] = values[:64]
    if position >= length or raw[position] != ":":
        return None
    return Line(name.upper(), params, raw[position + 1:], raw)


def unescape(value: str) -> str:
    """A TEXT value: ``\\n`` is a line break, ``\\,`` ``\\;`` ``\\\\`` the characters themselves."""
    if "\\" not in value:
        return value
    return re.sub(r"\\(.)", lambda match: "\n" if match.group(1) in "nN" else match.group(1), value, flags=re.S)


def escape(value: str) -> str:
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n"))


def fold(line: str) -> list[str]:
    """A content line as physical lines of at most 75 octets (UTF-8 sequences are not split)."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return [line]
    parts: list[str] = []
    start = 0
    limit = 75
    while start < len(data):
        end = min(len(data), start + limit)
        while end < len(data) and end > start and (data[end] & 0xC0) == 0x80:
            end -= 1
        parts.append(("" if not parts else " ") + data[start:end].decode("utf-8"))
        start = end
        limit = 74                              # the continuation's leading space counts
    return parts


def split_values(value: str) -> list[str]:
    """A comma-separated list (commas escaped with a backslash do not separate)."""
    return [part for part in re.split(r"(?<!\\),", value) if part.strip()]


# --- values ----------------------------------------------------------------------------------------------

def parse_duration(value: str) -> dt.timedelta | None:
    match = _DURATION.match(value.strip().upper())
    if not match or value.strip().upper() in ("P", "+P", "-P", "PT"):
        return None
    sign, weeks, days, hours, minutes, seconds = match.groups()
    try:
        span = dt.timedelta(weeks=int(weeks or 0), days=int(days or 0), hours=int(hours or 0),
                            minutes=int(minutes or 0), seconds=int(seconds or 0))
    except OverflowError:
        return None
    return -span if sign == "-" else span


def format_duration(span: dt.timedelta) -> str:
    seconds = int(span.total_seconds())
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    if seconds == 0:
        return "PT0S"
    weeks, rest = divmod(seconds, 7 * 86400)
    if rest == 0:
        return f"{sign}P{weeks}W"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    text = f"{sign}P" + (f"{days}D" if days else "")
    if hours or minutes or secs:
        text += "T" + (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "") + (f"{secs}S" if secs
                                                                                              else "")
    return text


def parse_offset(value: str) -> int | None:
    """``+0100`` or ``-053000`` as seconds east of UTC."""
    match = _OFFSET.match(value.strip())
    if not match:
        return None
    seconds = int(match.group(2)) * 3600 + int(match.group(3)) * 60 + int(match.group(4) or 0)
    if seconds > 18 * 3600:
        return None
    return -seconds if match.group(1) == "-" else seconds


def format_offset(seconds: int) -> str:
    sign = "-" if seconds < 0 else "+"
    seconds = abs(seconds)
    text = f"{sign}{seconds // 3600:02d}{seconds % 3600 // 60:02d}"
    return text + (f"{seconds % 60:02d}" if seconds % 60 else "")


def parse_moment(value: str) -> tuple[dt.date | dt.datetime, bool] | None:
    """A DATE or DATE-TIME value as (date or naive datetime, written in UTC)."""
    value = value.strip().upper()
    try:
        match = _DATE.match(value)
        if match:
            return dt.date(*(int(group) for group in match.groups())), False
        match = _DATETIME.match(value)
        if match:
            year, month, day, hour, minute, second = (int(group) for group in match.groups()[:6])
            return dt.datetime(year, month, day, hour, minute, min(second, 59)), bool(match.group(7))
    except ValueError:
        return None
    return None


def format_date(day: dt.date) -> str:
    return f"{day.year:04d}{day.month:02d}{day.day:02d}"


def format_datetime(moment: dt.datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%S") if moment.year >= 1000 else (
        f"{moment.year:04d}" + moment.strftime("%m%dT%H%M%S"))


# --- VTIMEZONE: reading ----------------------------------------------------------------------------------

@dataclass
class _Observance:
    start: dt.datetime                 # local wall time of the first onset, by the offset in force before it
    offset_from: int
    offset_to: int
    rule: recurrence.Rule | None = None
    rdates: list[dt.datetime] = field(default_factory=list)

    def latest_onset(self, local: dt.datetime) -> dt.datetime | None:
        """The last time this observance began at or before the wall time ``local``."""
        best = None
        for moment in [self.start, *self.rdates]:
            if moment <= local and (best is None or moment > best):
                best = moment
        if self.rule is not None and self.start <= local:
            offset = self.offset_from
            instant = lambda moment: (moment - timeutil.EPOCH).total_seconds() - offset  # noqa: E731
            try:
                skip = local - dt.timedelta(days=800)
            except OverflowError:
                skip = None
            for moment in recurrence.iterate(self.rule, self.start, skip_to=skip, instant=instant, budget=4000):
                if moment > local:
                    break
                if best is None or moment > best:
                    best = moment
        return best


@dataclass
class VTimezone:
    """A time zone as a file defines it."""
    tzid: str
    observances: list[_Observance] = field(default_factory=list)
    location: str = ""                 # X-LIC-LOCATION, when the writer gave the database name

    def offset_at(self, local: dt.datetime) -> int | None:
        """Seconds east of UTC at the wall time ``local``; None when the definition is empty."""
        best: tuple[dt.datetime, int] | None = None
        for observance in self.observances:
            onset = observance.latest_onset(local)
            if onset is not None and (best is None or onset > best[0]):
                best = (onset, observance.offset_to)
        if best is not None:
            return best[1]
        if self.observances:
            return min(self.observances, key=lambda item: item.start).offset_from
        return None


def _read_vtimezone(lines: list[Line], children: list[tuple[str, list[Line]]]) -> VTimezone | None:
    tzid = next((line.value.strip() for line in lines if line.name == "TZID"), "")
    if not tzid:
        return None
    zone = VTimezone(tzid[:256])
    zone.location = next((line.value.strip() for line in lines if line.name in ("X-LIC-LOCATION", "TZID-ALIAS-OF")),
                         "")
    for kind, body in children[:512]:
        if kind not in ("STANDARD", "DAYLIGHT"):
            continue
        fields = {line.name: line for line in reversed(body)}
        try:
            start = parse_moment(fields["DTSTART"].value)
            offset_from = parse_offset(fields["TZOFFSETFROM"].value)
            offset_to = parse_offset(fields["TZOFFSETTO"].value)
        except KeyError:
            continue
        if start is None or offset_from is None or offset_to is None:
            continue
        observance = _Observance(model.as_datetime(start[0]), offset_from, offset_to)
        if "RRULE" in fields:
            try:
                observance.rule = recurrence.parse_rrule(fields["RRULE"].value)
            except recurrence.RecurrenceError:
                observance.rule = None
        for line in body:
            if line.name == "RDATE":
                for part in split_values(line.value)[:500]:
                    moment = parse_moment(part.split("/")[0])
                    if moment is not None:
                        observance.rdates.append(model.as_datetime(moment[0]))
        zone.observances.append(observance)
    return zone


def _sample_times(year: int) -> list[dt.datetime]:
    return [dt.datetime(y, month, 15, 12) for y in (year - 1, year, year + 1) for month in range(1, 13)
            if dt.MINYEAR < y < dt.MAXYEAR]


def match_zone(definition: VTimezone, year: int) -> str:
    """The database zone whose offsets equal those of ``definition`` around ``year`` (``""``: none does)."""
    samples = _sample_times(year)
    wanted = [definition.offset_at(moment) for moment in samples]
    if not samples or any(offset is None for offset in wanted):
        return ""
    hints = [word for word in re.split(r"[^A-Za-z]+", definition.tzid) if len(word) > 3]

    def fits(name: str) -> bool:
        zone = timeutil.zone(name)
        if zone is None:
            return False
        for moment, offset in zip(samples, wanted):
            actual = moment.replace(tzinfo=zone).utcoffset()
            if actual is None or int(actual.total_seconds()) != offset:
                return False
        return True

    names = timeutil.zone_names()
    hinted = [name for name in names if any(word.lower() in name.lower() for word in hints)]
    for name in [*hinted, *_COMMON_ZONES, *names]:
        if fits(name):
            return name
    return ""


def resolve_tzid(tzid: str, definitions: dict[str, VTimezone], year: int) -> str:
    """A zone of the database for a TZID (``""`` when none can be found). See the module text for the order."""
    tzid = tzid.strip().strip('"')
    if not tzid or len(tzid) > 256:
        return ""
    if tzid.upper() in ("UTC", "Z", "GMT", "ETC/UTC", "ETC/GMT"):
        return "UTC"
    if timeutil.zone(tzid) is not None:
        return tzid
    parts = [part for part in tzid.split("/") if part]
    for size in (3, 2, 1):
        if len(parts) > size:
            candidate = "/".join(parts[-size:])
            if "/" in candidate and timeutil.zone(candidate) is not None:
                return candidate
    for candidate in WINDOWS_ZONES.get(tzid, ()):
        if timeutil.zone(candidate) is not None:
            return candidate
    definition = definitions.get(tzid)
    if definition is not None and definition.location and timeutil.zone(definition.location) is not None:
        return definition.location
    match = _GMT.match(tzid.upper())
    if match and not int(match.group(3) or 0) and int(match.group(2)) <= 14:
        hours = int(match.group(2))
        if hours == 0:
            return "UTC"
        name = f"Etc/GMT{'-' if match.group(1) == '+' else '+'}{hours}"     # the database's signs are reversed
        if timeutil.zone(name) is not None:
            return name
    if definition is not None and definition.observances:
        return match_zone(definition, year)
    return ""


# --- reading ---------------------------------------------------------------------------------------------

@dataclass
class Parsed:
    """What a file held."""
    name: str = ""                                   # X-WR-CALNAME
    events: list[model.Event] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)     # reason -> how many
    warnings: list[str] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


@dataclass
class _Component:
    kind: str
    lines: list[Line] = field(default_factory=list)
    children: list["_Component"] = field(default_factory=list)


@dataclass
class _Raw:
    """A VEVENT before its times are put into the terms of its series."""
    event: model.Event
    rid: tuple[dt.date | dt.datetime, bool, str] | None      # (value, in UTC, zone name) of RECURRENCE-ID
    zone_offset: int | None                                  # for times converted to UTC by a VTIMEZONE


def parse(data: bytes | str) -> Parsed:
    """Read an iCalendar file. Raises ICalError when it is none (or too large); see the module text."""
    if isinstance(data, bytes):
        if len(data) > MAX_FILE_BYTES:
            raise ICalError(f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
        text = decode(data)
    else:
        text = data
    result = Parsed()
    stack: list[_Component] = []
    roots: list[_Component] = []
    seen_calendar = False
    events = 0
    for raw in unfold(text):
        if not raw.strip():
            continue
        line = parse_line(raw)
        if line is None:
            continue
        if line.name == "BEGIN":
            kind = line.value.strip().upper()
            if len(stack) >= MAX_DEPTH:
                raise ICalError("The file nests components too deeply to be a calendar.")
            if kind == "VCALENDAR":
                seen_calendar = True
            if kind == "VEVENT":
                events += 1
            component = _Component(kind)
            if stack:
                if not (kind == "VEVENT" and events > MAX_EVENTS):
                    stack[-1].children.append(component)
            else:
                roots.append(component)
            stack.append(component)
        elif line.name == "END":
            kind = line.value.strip().upper()
            while stack:
                if stack.pop().kind == kind:
                    break
        elif stack:
            if len(stack[-1].lines) < 4096:
                stack[-1].lines.append(line)
    if not seen_calendar and not any(root.kind == "VEVENT" for root in roots):
        raise ICalError("This is not an iCalendar file: it has no VCALENDAR.")
    if events > MAX_EVENTS:
        result.warnings.append(f"The file has more than {MAX_EVENTS} events; the rest was left out.")

    definitions: dict[str, VTimezone] = {}
    components: list[_Component] = []
    for root in roots:
        if root.kind == "VCALENDAR":
            if not result.name:
                result.name = next((unescape(line.value).strip()[:80] for line in root.lines
                                    if line.name == "X-WR-CALNAME"), "")
            components.extend(root.children)
        else:
            components.append(root)                       # a bare VEVENT: some programs write that
    for component in components:
        if component.kind == "VTIMEZONE" and len(definitions) < MAX_VTIMEZONES:
            zone = _read_vtimezone(component.lines, [(child.kind, child.lines) for child in component.children])
            if zone is not None:
                definitions.setdefault(zone.tzid, zone)
    raws: list[_Raw] = []
    zones = _Zones(definitions, result)
    for component in components:
        if component.kind == "VEVENT":
            if len(raws) >= MAX_EVENTS:
                break
            raw_event = _read_event(component, zones, result)
            if raw_event is not None:
                raws.append(raw_event)
        elif component.kind in ("VTODO", "VJOURNAL", "VFREEBUSY"):
            result.skip({"VTODO": "tasks", "VJOURNAL": "journal entries", "VFREEBUSY": "free/busy entries"}[
                component.kind])
    result.events = _link_series(raws, result)
    return result


class _Zones:
    """Turns the time values of one file into local times with a zone of the database."""

    def __init__(self, definitions: dict[str, VTimezone], result: Parsed) -> None:
        self.definitions = definitions
        self.result = result
        self._resolved: dict[tuple[str, int], str] = {}

    def moment(self, line: Line) -> tuple[dt.date | dt.datetime, str, bool] | None:
        """(value, zone, converted): a date; or a wall time with ``""`` (floating), ``UTC`` or a zone name.

        ``converted`` is True when the TZID was unknown and the time was put into UTC.
        """
        parsed = parse_moment(line.value)
        if parsed is None:
            return None
        value, in_utc = parsed
        if not isinstance(value, dt.datetime):
            return value, "", False
        if in_utc:
            return value, "UTC", False
        tzid = line.param("TZID")
        if not tzid:
            return value, "", False
        key = (tzid, value.year // 4)
        if key not in self._resolved:
            self._resolved[key] = resolve_tzid(tzid, self.definitions, value.year)
        zone = self._resolved[key]
        if zone:
            return value, zone, False
        definition = self.definitions.get(tzid.strip().strip('"'))
        offset = definition.offset_at(value) if definition is not None else None
        if offset is None:
            self.result.skip("times in an unknown time zone (kept as local times)")
            return value, "", False
        try:
            return value - dt.timedelta(seconds=offset), "UTC", True
        except OverflowError:
            return None


def _stamp(line: Line | None) -> int:
    if line is None:
        return 0
    parsed = parse_moment(line.value)
    if parsed is None:
        return 0
    try:
        return max(0, int(model.as_datetime(parsed[0]).replace(tzinfo=UTC).timestamp()))
    except (OverflowError, ValueError):
        return 0


def _in_zone(value: dt.date | dt.datetime, zone: str, target: str, all_day: bool) -> dt.date | dt.datetime:
    """A time read from the file, in the terms of an event that is all-day or lives in the zone ``target``."""
    if all_day:
        return value.date() if isinstance(value, dt.datetime) else value
    moment = model.as_datetime(value)
    if zone == target or not zone or not target:
        return moment                                # same zone, or a floating time on either side
    source_zone = UTC if zone == "UTC" else timeutil.zone(zone)
    target_zone = UTC if target == "UTC" else timeutil.zone(target)
    if source_zone is None or target_zone is None:
        return moment
    return timeutil.from_epoch(timeutil.to_epoch(moment, source_zone), target_zone)


def _read_event(component: _Component, zones: "_Zones", result: Parsed) -> _Raw | None:
    first: dict[str, Line] = {}
    for line in component.lines:
        first.setdefault(line.name, line)
    start_line = first.get("DTSTART")
    start = zones.moment(start_line) if start_line is not None else None
    if start is None:
        result.skip("events without a readable start")
        return None
    start_value, tz, _converted = start
    all_day = not isinstance(start_value, dt.datetime)
    end_value: dt.date | dt.datetime | None = None
    if "DTEND" in first:
        end = zones.moment(first["DTEND"])
        if end is not None:
            end_value = _in_zone(end[0], end[1], tz, all_day)
    elif "DURATION" in first:
        span = parse_duration(first["DURATION"].value)
        if span is not None and span >= dt.timedelta(0):
            try:
                if all_day:
                    end_value = start_value + dt.timedelta(days=max(1, -(-int(span.total_seconds()) // 86400)))
                else:
                    end_value = start_value + span
            except OverflowError:
                end_value = None
    try:
        if end_value is None:
            end_value = start_value + model.DAY if all_day else start_value
        elif all_day and end_value <= start_value:
            end_value = start_value + model.DAY
    except OverflowError:
        result.skip("events with dates out of range")
        return None

    rrule = ""
    rules = [line for line in component.lines if line.name == "RRULE"]
    if rules:
        try:
            rule = recurrence.parse_rrule(rules[0].value)
            rrule = recurrence.format_rrule(_rule_for(rule, all_day, tz))
        except recurrence.RecurrenceError:
            result.skip("repeat rules that could not be read (the event was kept without repeating)")
        if len(rules) > 1:
            result.skip("additional repeat rules of an event (only the first is used)")

    def moments(name: str) -> list[str]:
        found = []
        for line in component.lines:
            if line.name != name:
                continue
            for part in split_values(line.value)[:model.MAX_DATES]:
                value = zones.moment(replace(line, value=part.split("/")[0]))
                if value is not None:
                    found.append(model.format_local(_in_zone(value[0], value[1], tz, all_day)))
        return found[:model.MAX_DATES]

    rid = None
    if "RECURRENCE-ID" in first:
        value = zones.moment(first["RECURRENCE-ID"])
        if value is None:
            result.skip("changed occurrences without a readable original time")
            return None
        rid = (value[0], False, value[1])

    summary = unescape(first["SUMMARY"].value) if "SUMMARY" in first else ""
    uid = unescape(first["UID"].value).strip() if "UID" in first else ""
    if not uid or len(uid) > 512 or any(ord(character) < 0x20 for character in uid):
        # No usable UID: derive one from the content, so the same file gives the same events again.
        digest = hashlib.sha256("\n".join(line.raw for line in component.lines if line.name != "DTSTAMP")
                                .encode("utf-8", "replace")).hexdigest()[:32]
        uid = f"{digest}@imported"
    reminders = _read_alarms(component, start_value, end_value, bool(rrule) or bool(rid), tz)
    extra = [line.raw for line in component.lines
             if line.name not in _KNOWN and len(line.raw) <= model.MAX_EXTRA_LINE][:model.MAX_EXTRA_LINES]
    event = model.make_event({
        "uid": uid, "calendar": "", "summary": summary,
        "location": unescape(first["LOCATION"].value) if "LOCATION" in first else "",
        "description": unescape(first["DESCRIPTION"].value) if "DESCRIPTION" in first else "",
        "all_day": all_day, "start": model.format_local(start_value), "end": model.format_local(end_value),
        "tz": tz, "rrule": rrule, "rdates": moments("RDATE"), "exdates": moments("EXDATE"),
        "reminders": reminders, "created": _stamp(first.get("CREATED")),
        "modified": _stamp(first.get("LAST-MODIFIED")) or _stamp(first.get("DTSTAMP")),
        "sequence": _sequence(first.get("SEQUENCE")), "extra": extra})
    if event is None:
        result.skip("events that could not be read")
        return None
    return _Raw(event, rid, None)


def _sequence(line: Line | None) -> int:
    if line is None or not line.value.strip().isdigit():
        return 0
    return min(int(line.value.strip()[:10]), 2 ** 31 - 1)


def _rule_for(rule: recurrence.Rule, all_day: bool, tz: str) -> recurrence.Rule:
    """``rule`` with its UNTIL in the form the event's times have (a date, a wall time, or UTC)."""
    until = rule.until
    if until is None:
        return rule
    if all_day:
        day = (until.astimezone(UTC) if until.tzinfo is not None else until).date()
        return replace(rule, until=dt.datetime(day.year, day.month, day.day, 23, 59, 59), until_is_date=True)
    if rule.until_is_date:
        return rule                                   # the whole of that day, in the event's wall time
    if tz and until.tzinfo is None:
        zone = UTC if tz == "UTC" else timeutil.zone(tz)
        if zone is not None:
            return replace(rule, until=dt.datetime.fromtimestamp(timeutil.to_epoch(until, zone), UTC))
    if not tz and until.tzinfo is not None:
        return replace(rule, until=until.astimezone(UTC).replace(tzinfo=None))
    return rule


def _read_alarms(component: _Component, start: dt.date | dt.datetime, end: dt.date | dt.datetime,
                 repeats: bool, tz: str) -> list[int]:
    minutes: list[int] = []
    for child in component.children[:32]:
        if child.kind != "VALARM":
            continue
        fields = {line.name: line for line in reversed(child.lines)}
        action = fields["ACTION"].value.strip().upper() if "ACTION" in fields else "DISPLAY"
        trigger = fields.get("TRIGGER")
        if action not in ("DISPLAY", "AUDIO") or trigger is None:
            continue
        span = parse_duration(trigger.value)
        if span is not None:
            seconds = -span.total_seconds()                        # positive: before
            if trigger.param("RELATED").upper() == "END":
                seconds -= (model.as_datetime(end) - model.as_datetime(start)).total_seconds()
        else:
            moment = parse_moment(trigger.value)
            if moment is None or repeats or not isinstance(moment[0], dt.datetime):
                continue
            zone = UTC if tz == "UTC" or not tz else timeutil.zone(tz)
            if not moment[1] or zone is None:
                continue
            seconds = timeutil.to_epoch(model.as_datetime(start), zone) - moment[0].replace(tzinfo=UTC).timestamp()
        value = int(round(seconds / 60))
        if abs(value) <= model.MAX_REMINDER_MINUTES:
            minutes.append(value)
    return minutes


def _link_series(raws: list[_Raw], result: Parsed) -> list[model.Event]:
    """Give every changed occurrence its recurrence ID in the terms of its master; drop duplicates."""
    masters: dict[str, model.Event] = {}
    for raw in raws:
        if raw.rid is None:
            if raw.event.uid in masters:
                result.skip("events with a UID that occurs twice (the later one was used)")
            masters[raw.event.uid] = raw.event
    events: dict[tuple[str, str], model.Event] = {}
    for raw in raws:
        if raw.rid is None:
            if masters.get(raw.event.uid) is raw.event:
                events[raw.event.key] = raw.event
            continue
        master = masters.get(raw.event.uid)
        value, _in_utc, zone = raw.rid
        if master is not None:
            value = _in_zone(value, zone, master.tz, master.all_day)
        event = replace(raw.event, recurrence_id=model.format_local(value), rrule="", rdates=(), exdates=())
        events[event.key] = event
    return list(events.values())


def read_file(path: str) -> Parsed:
    """Read the ``.ics`` file at ``path`` (at most ``MAX_FILE_BYTES``). Raises ICalError."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise ICalError(f"The file cannot be read: {exc.strerror or exc}") from exc
    return parse(data)


# --- VTIMEZONE: writing ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Transition:
    instant: int                       # seconds since the epoch at which the offset changes
    offset_from: int
    offset_to: int
    name: str
    daylight: bool

    @property
    def local(self) -> dt.datetime:
        """The wall time of the onset, by the offset in force before it (as DTSTART wants it)."""
        return timeutil.EPOCH + dt.timedelta(seconds=self.instant + self.offset_from)


def _state(zone: dt.tzinfo, instant: int) -> tuple[int, str, bool]:
    moment = dt.datetime.fromtimestamp(instant, zone)
    offset = moment.utcoffset() or dt.timedelta(0)
    return int(offset.total_seconds()), moment.tzname() or "", bool(moment.dst())


def transitions(zone: dt.tzinfo, first_year: int, last_year: int) -> list[_Transition]:
    """The changes of offset of ``zone`` from the start of ``first_year`` to the end of ``last_year``."""
    first_year = max(2, first_year)
    last_year = min(dt.MAXYEAR - 1, last_year)
    begin = int(dt.datetime(first_year, 1, 1, tzinfo=UTC).timestamp())
    end = int(dt.datetime(last_year + 1, 1, 1, tzinfo=UTC).timestamp())
    found: list[_Transition] = []
    step = 86400
    low = begin
    before = _state(zone, low)
    while low < end:
        high = min(end, low + step)
        after = _state(zone, high)
        if after != before:
            left, right = low, high
            while right - left > 1:
                middle = (left + right) // 2
                if _state(zone, middle) == before:
                    left = middle
                else:
                    right = middle
            now = _state(zone, right)
            found.append(_Transition(right, before[0], now[0], now[1], now[2]))
            low, before = right, now
            continue                    # the same day may hold another change
        low, before = high, after
    return found


def _pattern(transition: _Transition) -> tuple:
    local = transition.local
    length = _calendar.monthrange(local.year, local.month)[1]
    return (transition.offset_from, transition.offset_to, transition.name, transition.daylight, local.month,
            local.weekday(), local.time(), (local.day - 1) // 7 + 1, local.day + 7 > length)


def vtimezone(name: str, first_year: int, last_year: int) -> list[str]:
    """The content lines of a VTIMEZONE for the database zone ``name`` (not folded), or [] for UTC or unknown."""
    zone = timeutil.zone(name)
    if zone is None or name == "UTC":
        return []
    first_year = max(1970, min(first_year, last_year))
    last_year = max(first_year, min(last_year, dt.MAXYEAR - 5))
    changes = transitions(zone, first_year, last_year + 3)       # three more years show whether a rule goes on
    lines = ["BEGIN:VTIMEZONE", f"TZID:{name}", f"X-LIC-LOCATION:{name}"]

    def block(kind: str, start: dt.datetime, offset_from: int, offset_to: int, label: str, rule: str = "") -> None:
        lines.append(f"BEGIN:{kind}")
        lines.append(f"DTSTART:{format_datetime(start)}")
        lines.append(f"TZOFFSETFROM:{format_offset(offset_from)}")
        lines.append(f"TZOFFSETTO:{format_offset(offset_to)}")
        if label:
            lines.append(f"TZNAME:{escape(label)}")
        if rule:
            lines.append(f"RRULE:{rule}")
        lines.append(f"END:{kind}")

    opening = _state(zone, int(dt.datetime(first_year, 1, 1, tzinfo=UTC).timestamp()))
    block("DAYLIGHT" if opening[2] else "STANDARD", dt.datetime(1970, 1, 1), opening[0], opening[0], opening[1])
    # The yearly rule: the transitions of the last years, when each of them has come the same way every year.
    ruled: set[int] = set()
    by_year: dict[int, list[_Transition]] = {}
    for change in changes:
        by_year.setdefault(change.local.year, []).append(change)
    final_year = last_year + 3
    for anchor in by_year.get(final_year, []):
        wanted = _pattern(anchor)
        last_weekday = wanted[8]
        series = []
        year = final_year
        while year >= first_year:
            same = [change for change in by_year.get(year, [])
                    if _pattern(change)[:7] == wanted[:7]
                    and (_pattern(change)[8] if last_weekday else _pattern(change)[7] == wanted[7])]
            if len(same) != 1:
                break
            series.append(same[0])
            year -= 1
        if len(series) < 3:
            continue
        earliest = series[-1]
        ordinal = -1 if last_weekday else wanted[7]
        rule = f"FREQ=YEARLY;BYMONTH={wanted[4]};BYDAY={ordinal}{recurrence.WEEKDAY_CODES[wanted[5]]}"
        block("DAYLIGHT" if anchor.daylight else "STANDARD", earliest.local, anchor.offset_from, anchor.offset_to,
              anchor.name, rule)
        ruled.update(id(change) for change in series)
    for change in changes:
        if id(change) not in ruled and change.local.year <= last_year:
            block("DAYLIGHT" if change.daylight else "STANDARD", change.local, change.offset_from, change.offset_to,
                  change.name)
    lines.append("END:VTIMEZONE")
    return lines


# --- writing ---------------------------------------------------------------------------------------------

def _moment_line(name: str, value: dt.date | dt.datetime, tz: str) -> str:
    if not isinstance(value, dt.datetime):
        return f"{name};VALUE=DATE:{format_date(value)}"
    if tz == "UTC":
        return f"{name}:{format_datetime(value)}Z"
    if tz:
        return f"{name};TZID={tz}:{format_datetime(value)}"
    return f"{name}:{format_datetime(value)}"


def _list_line(name: str, values: Iterable[str], all_day: bool, tz: str) -> str | None:
    moments = []
    for text in values:
        try:
            moments.append(model.parse_local(text))
        except ValueError:
            continue
    if not moments:
        return None
    if all_day:
        return f"{name};VALUE=DATE:" + ",".join(format_date(m.date() if isinstance(m, dt.datetime) else m)
                                                 for m in moments)
    texts = [format_datetime(model.as_datetime(m)) for m in moments]
    if tz == "UTC":
        return f"{name}:" + ",".join(text + "Z" for text in texts)
    if tz:
        return f"{name};TZID={tz}:" + ",".join(texts)
    return f"{name}:" + ",".join(texts)


def _stamp_text(stamp: int) -> str:
    return format_datetime(dt.datetime.fromtimestamp(stamp, UTC).replace(tzinfo=None)) + "Z"


def event_lines(event: model.Event, master: model.Event | None, now: int) -> list[str]:
    """The content lines of one VEVENT (not folded)."""
    lines = ["BEGIN:VEVENT", f"UID:{escape(event.uid)}", f"DTSTAMP:{_stamp_text(now)}"]
    start, end = event.start_value, event.end_value
    lines.append(_moment_line("DTSTART", start, event.tz))
    lines.append(_moment_line("DTEND", end, event.tz))
    if event.recurrence_id:
        owner = master if master is not None else event
        try:
            value = model.parse_local(event.recurrence_id)
            if owner.all_day and isinstance(value, dt.datetime):
                value = value.date()
            lines.append(_moment_line("RECURRENCE-ID", value, "" if owner.all_day else owner.tz))
        except ValueError:
            pass
    if event.summary:
        lines.append(f"SUMMARY:{escape(event.summary)}")
    if event.location:
        lines.append(f"LOCATION:{escape(event.location)}")
    if event.description:
        lines.append(f"DESCRIPTION:{escape(event.description)}")
    rule = event.rule()
    if rule is not None:
        lines.append("RRULE:" + recurrence.format_rrule(_rule_for(rule, event.all_day, event.tz)))
    for name, values in (("RDATE", event.rdates), ("EXDATE", event.exdates)):
        line = _list_line(name, values, event.all_day, event.tz)
        if line is not None:
            lines.append(line)
    if event.created:
        lines.append(f"CREATED:{_stamp_text(event.created)}")
    if event.modified:
        lines.append(f"LAST-MODIFIED:{_stamp_text(event.modified)}")
    if event.sequence:
        lines.append(f"SEQUENCE:{event.sequence}")
    for raw in event.extra:
        parsed = parse_line(raw)
        if parsed is not None and parsed.name not in _KNOWN and parsed.name not in ("BEGIN", "END"):
            lines.append(raw)
    for minutes in event.reminders:
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{escape(event.summary or 'Reminder')}",
                  f"TRIGGER:{format_duration(dt.timedelta(minutes=-minutes))}", "END:VALARM"]
    lines.append("END:VEVENT")
    return lines


def export(events: Iterable[model.Event], *, name: str = "", now: int | None = None) -> bytes:
    """An iCalendar file with ``events`` (masters and their changed occurrences), as bytes with CRLF lines."""
    events = list(events)
    stamp = model.now_stamp() if now is None else now
    masters = {event.uid: event for event in events if not event.recurrence_id}
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN"]
    if name:
        lines.append(f"X-WR-CALNAME:{escape(name)}")
    spans: dict[str, list[int]] = {}
    this_year = dt.datetime.fromtimestamp(stamp, UTC).year
    for event in events:
        if event.all_day or not event.tz or event.tz == "UTC":
            continue
        span = spans.setdefault(event.tz, [event.start_dt.year, event.end_dt.year])
        span[0] = min(span[0], event.start_dt.year)
        last = event.end_dt.year
        rule = event.rule()
        if rule is not None:
            if rule.until is not None:
                last = max(last, rule.until.year)
            else:
                last = max(last, this_year + 2)
        for text in event.rdates:
            last = max(last, model.as_datetime(model.parse_local(text)).year)
        span[1] = max(span[1], last)
    for tz in sorted(spans):
        lines += vtimezone(tz, spans[tz][0] - 1, min(spans[tz][1], spans[tz][0] + 60))
    for event in events:
        lines += event_lines(event, masters.get(event.uid), stamp)
    lines.append("END:VCALENDAR")
    physical: list[str] = []
    for line in lines:
        physical += fold(line.replace("\r", "").replace("\n", " ").replace("\0", ""))
    return ("\r\n".join(physical) + "\r\n").encode("utf-8")
