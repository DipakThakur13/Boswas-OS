"""Date, time, time zone and system locale: what systemd's tools print, without Qt.

  timedatectl show           Timezone=, NTP=, CanNTP=, NTPSynchronized= (key=value lines)
  zoneinfo                   the names of the tz database (the standard library)
  localectl status           "System Locale: LANG=..." and the LC_* lines under it
  locale -a                  the locales that are generated on this system
  /usr/share/i18n/locales/   each locale's language and territory in words

Changes are made by timedatectl and localectl themselves (commands.py builds
their argument lists); systemd's services ask polkit. Everything parsed here
is validated against the same patterns the argument builders use, so a name
read from a tool can be handed back to it.
"""

from __future__ import annotations

import re
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, timezone

from .commands import LOCALE_RE, TIMEZONE_RE

I18N_LOCALES = "/usr/share/i18n/locales"
# The variables that make up the "regional format" (dates, numbers, money, paper, units).
FORMAT_VARIABLES = ("LC_TIME", "LC_NUMERIC", "LC_MONETARY", "LC_MEASUREMENT", "LC_PAPER")
MAX_LOCALES = 2000
_ZONE_AREAS = ("Africa", "America", "Antarctica", "Arctic", "Asia", "Atlantic", "Australia", "Europe", "Indian",
               "Pacific", "Etc")


@dataclass(frozen=True)
class TimeState:
    timezone: str = ""           # "" when timedatectl did not say
    ntp: bool = False            # the time is set automatically
    can_ntp: bool = False        # a time synchronisation service is installed
    synchronized: bool = False


def _yes(value: str) -> bool:
    return value.strip().lower() in ("yes", "true", "1")


def parse_timedatectl(text: object) -> TimeState:
    """The state from `timedatectl show`."""
    values: dict[str, str] = {}
    if isinstance(text, str):
        for line in text.splitlines()[:200]:
            key, sep, value = line.partition("=")
            if sep and key.isalnum():
                values[key] = value.strip()[:128]
    zone = values.get("Timezone", "")
    return TimeState(zone if valid_timezone(zone) else "", _yes(values.get("NTP", "")),
                     _yes(values.get("CanNTP", "")), _yes(values.get("NTPSynchronized", "")))


def valid_timezone(name: object) -> bool:
    return isinstance(name, str) and bool(TIMEZONE_RE.fullmatch(name)) and ".." not in name


def timezones(names=None) -> list[str]:
    """The time zones offered: the geographic names of the tz database and UTC, sorted."""
    if names is None:
        try:
            names = zoneinfo.available_timezones()
        except Exception:                 # no tz database at all
            names = ()
    chosen = {name for name in names if isinstance(name, str) and valid_timezone(name)
              and name.split("/")[0] in _ZONE_AREAS and "/" in name and not name.startswith("Etc/GMT")
              and name not in ("Etc/Zulu", "Etc/Universal", "Etc/Greenwich", "Etc/UCT")}
    chosen.discard("Etc/UTC")
    return ["UTC", *sorted(chosen - {"UTC"})]


def zone_words(zone: str) -> str:
    """ "America/Argentina/Buenos_Aires" -> "Buenos Aires, Argentina (America)"."""
    parts = [part.replace("_", " ") for part in zone.split("/")]
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(reversed(parts[1:]))} ({parts[0]})"


def utc_offset(zone: str, now: datetime | None = None) -> str:
    """ "UTC+01:00" for the zone right now; "" when the tz database does not have it."""
    try:
        info = zoneinfo.ZoneInfo(zone)
    except Exception:
        return ""
    moment = (now or datetime.now(timezone.utc)).astimezone(info)
    offset = moment.utcoffset()
    if offset is None:
        return ""
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def zone_label(zone: str, now: datetime | None = None) -> str:
    offset = utc_offset(zone, now)
    return f"{zone_words(zone)} · {offset}" if offset else zone_words(zone)


def search_timezones(query: str, zones: list[str]) -> list[str]:
    """The zones whose name contains every word of the query ("new york", "berlin", "asia ko")."""
    words = query.casefold().replace("_", " ").replace("/", " ").split()
    if not words:
        return list(zones)
    return [zone for zone in zones if all(word in zone.casefold().replace("_", " ") for word in words)]


def valid_manual_time(text: object) -> bool:
    """ "YYYY-MM-DD HH:MM:SS" naming a real moment between 2000 and 2099 (what set-time is given)."""
    if not isinstance(text, str):
        return False
    try:
        moment = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return 2000 <= moment.year <= 2099 and moment.strftime("%Y-%m-%d %H:%M:%S") == text


# --- locale -------------------------------------------------------------------------------------------------

def normalise_locale(name: object) -> str | None:
    """ "de_DE.utf8" -> "de_DE.UTF-8"; None for a name that is not a UTF-8 locale of language_TERRITORY form."""
    if not isinstance(name, str):
        return None
    name = name.strip()
    match = re.fullmatch(r"([a-z]{2,3}_[A-Z]{2})\.(?:utf8|UTF-8)(@[A-Za-z0-9]{1,16})?", name)
    if match is None:
        return "C.UTF-8" if name in ("C.utf8", "C.UTF-8") else None
    result = f"{match.group(1)}.UTF-8{match.group(2) or ''}"
    return result if LOCALE_RE.fullmatch(result) else None


def parse_locale_list(text: object) -> list[str]:
    """The generated UTF-8 locales from `locale -a`, normalised and sorted (C.UTF-8 last)."""
    found: set[str] = set()
    if isinstance(text, str):
        for line in text.splitlines()[:MAX_LOCALES * 4]:
            locale = normalise_locale(line)
            if locale is not None:
                found.add(locale)
            if len(found) >= MAX_LOCALES:
                break
    return sorted(found - {"C.UTF-8"}) + (["C.UTF-8"] if "C.UTF-8" in found else [])


def parse_localectl(text: object) -> dict[str, str]:
    """{variable: locale} of the "System Locale:" block of `localectl status` (valid entries only)."""
    values: dict[str, str] = {}
    if not isinstance(text, str):
        return values
    inside = False
    for line in text.splitlines()[:100]:
        stripped = line.strip()
        if stripped.startswith("System Locale:"):
            inside, stripped = True, stripped[len("System Locale:"):].strip()
        elif inside and ":" in stripped.split("=")[0]:
            break                         # the next block ("VC Keymap: ...")
        if not inside:
            continue
        name, sep, value = stripped.partition("=")
        value = value.strip().strip('"')
        if sep and (name == "LANG" or name.startswith("LC_")) and LOCALE_RE.fullmatch(value):
            values[name] = value
    return values


@dataclass(frozen=True)
class LocaleState:
    language: str = ""                    # LANG
    formats: str = ""                     # the regional format: LC_TIME (or LANG when no LC_TIME is set)
    mixed: bool = False                   # the format variables do not all name the same locale


def locale_state(values: dict[str, str]) -> LocaleState:
    language = normalise_locale(values.get("LANG", "")) or values.get("LANG", "")
    chosen = [values[name] for name in FORMAT_VARIABLES if name in values]
    formats = (normalise_locale(values.get("LC_TIME", "")) or values.get("LC_TIME", "")) or language
    mixed = len({normalise_locale(v) or v for v in chosen}) > 1 or (bool(chosen) and len(chosen) != len(FORMAT_VARIABLES)
                                                                    and any((normalise_locale(v) or v) != language
                                                                            for v in chosen))
    return LocaleState(language, formats, mixed)


def set_locale_words(language: str, formats: str) -> tuple[str, ...]:
    """The words after `localectl set-locale` for a language and a regional format."""
    if not LOCALE_RE.fullmatch(language) or not LOCALE_RE.fullmatch(formats):
        raise ValueError("not a locale name")
    if formats == language:
        return (f"LANG={language}",)
    return (f"LANG={language}", *(f"{name}={formats}" for name in FORMAT_VARIABLES))


_IDENT_RE = re.compile(r'^(language|territory)\s+"([^"]{0,200})"', re.MULTILINE)
_UCODE_RE = re.compile(r"<U([0-9A-Fa-f]{4,8})>")


def _decode_identification(value: str) -> str:
    def char(match: re.Match) -> str:
        try:
            return chr(int(match.group(1), 16))
        except (ValueError, OverflowError):
            return ""
    text = _UCODE_RE.sub(char, value)
    return "".join(ch for ch in text if ch.isprintable()).strip()[:80]


def identification(text: object) -> tuple[str, str]:
    """(language, territory) in English from a locale definition file of /usr/share/i18n/locales."""
    found = {"language": "", "territory": ""}
    if isinstance(text, str):
        for key, value in _IDENT_RE.findall(text[:65536]):
            if not found[key]:
                found[key] = _decode_identification(value)
    return found["language"], found["territory"]


def locale_label(locale: str, names: tuple[str, str] | None = None) -> str:
    """ "German (Germany)" when the names are known, else the locale's own name without the encoding."""
    if locale == "C.UTF-8":
        return "No regional format (C)"
    base, _dot, rest = locale.partition(".")
    variant = f", {rest.partition('@')[2]}" if "@" in rest else ""
    if names and names[0]:
        return f"{names[0]} ({names[1]}{variant})" if names[1] else f"{names[0]}{variant}"
    return base + (f" ({rest.partition('@')[2]})" if "@" in rest else "")


def read_locale_names(locales: list[str], root: str = "/") -> dict[str, tuple[str, str]]:
    """{locale: (language, territory)} read from the locale definition files that exist (blocking)."""
    names: dict[str, tuple[str, str]] = {}
    base = root.rstrip("/") + I18N_LOCALES
    for locale in locales[:MAX_LOCALES]:
        stem, _dot, rest = locale.partition(".")
        variant = rest.partition("@")[2]
        if not re.fullmatch(r"[a-z]{2,3}_[A-Z]{2}", stem):
            continue
        for candidate in ((f"{stem}@{variant}",) if variant else ()) + (stem,):
            try:
                with open(f"{base}/{candidate}", "rb") as handle:
                    found = identification(handle.read(65536).decode("utf-8", "replace"))
            except OSError:
                continue
            if found[0]:
                names[locale] = found
                break
    return names
