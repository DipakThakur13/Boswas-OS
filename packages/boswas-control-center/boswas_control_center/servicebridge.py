"""The wire format of the services bridge, without Qt.

/usr/libexec/boswas/boswas-services-bridge runs the shell's own backends for
NetworkManager, BlueZ, the sound server, UPower / power-profiles-daemon and
the backlight, so that Settings and the quick settings panel show the same
state and behave the same. It speaks JSON lines: every event on its standard
output carries one service's WHOLE state, and commands go to its standard
input (boswas-services-bridge/src/protocol.rs):

    {"event":"ready","version":"1.0.0"}
    {"event":"network","available":true,"wifi_enabled":true,"access_points":[...],...}
    {"event":"bluetooth",...}  {"event":"audio",...}  {"event":"power",...}
    {"event":"brightness","level":0.8}
    {"event":"error","message":"..."}            a command it did not understand

    {"cmd":"wifi-connect","ssid":"Home","passphrase":"..."}

This module turns event lines into plain frozen dataclasses (Decoder) and
command dictionaries into lines (encode). Parsing is tolerant: unknown
fields and unknown events are ignored, a value of the wrong type becomes the
field's default, lists and texts are capped, and a line that is too long,
not UTF-8 or not a JSON object is dropped and counted. encode() is strict:
it only lets through the commands of the bridge's table with values the
bridge accepts, so a mistake shows up here and not as an "error" event.

A Wi-Fi passphrase exists only inside the command dictionary and the line
written to the pipe; describe() gives the text of a command for a log
without it.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace

PROGRAM = "/usr/libexec/boswas/boswas-services-bridge"

NETWORK, BLUETOOTH, AUDIO, POWER, BRIGHTNESS = "network", "bluetooth", "audio", "power", "brightness"
SERVICES = (NETWORK, BLUETOOTH, AUDIO, POWER, BRIGHTNESS)

MAX_LINE = 256 * 1024          # an event line longer than this is dropped
MAX_COMMAND = 16 * 1024        # the longest line the bridge reads
MAX_SSID_BYTES = 32
MAX_SECRET_BYTES = 256
MAX_ACCESS_POINTS = 256
MAX_DEVICES = 128
MAX_TEXT = 256
MAX_VOLUME = 1.5

CONNECTIVITY = ("none", "limited", "portal", "full")
PROFILES = ("power-saver", "balanced", "performance")
ADDRESS_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


# --- states ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AccessPoint:
    ssid: str
    strength: int = 0            # 0 to 100
    secured: bool = False
    known: bool = False          # NetworkManager has a profile: connecting needs no passphrase
    connected: bool = False
    connecting: bool = False


@dataclass(frozen=True)
class NetworkState:
    available: bool = False      # NetworkManager is running and answering
    connectivity: str = "none"   # none, limited, portal, full
    wired: bool = False
    wifi_present: bool = False
    wifi_enabled: bool = False
    wifi_connected: str | None = None
    wifi_scanning: bool = False
    access_points: tuple[AccessPoint, ...] = ()
    vpn: bool = False
    airplane: bool = False
    error: str | None = None     # the last failed connection attempt, in the bridge's words


@dataclass(frozen=True)
class BluetoothDevice:
    address: str                 # AA:BB:CC:DD:EE:FF
    name: str = ""
    icon: str = ""               # the freedesktop icon name BlueZ suggests, or empty
    paired: bool = False
    connected: bool = False
    connecting: bool = False
    battery: int | None = None   # 0 to 100


@dataclass(frozen=True)
class BluetoothState:
    available: bool = False      # BlueZ is running and there is an adapter
    enabled: bool = False
    discovering: bool = False
    devices: tuple[BluetoothDevice, ...] = ()


@dataclass(frozen=True)
class AudioDevice:
    id: int
    name: str = ""
    volume: float = 0.0          # 0.0 to 1.5 (above 1.0 is amplified)
    muted: bool = False
    default: bool = False


@dataclass(frozen=True)
class AudioState:
    available: bool = False      # the sound server answers
    output: AudioDevice | None = None
    input: AudioDevice | None = None
    outputs: tuple[AudioDevice, ...] = ()
    inputs: tuple[AudioDevice, ...] = ()


@dataclass(frozen=True)
class Battery:
    percent: int = 0
    charging: bool = False
    full: bool = False
    seconds_left: int | None = None      # until empty (discharging) or full (charging)


@dataclass(frozen=True)
class PowerState:
    battery: Battery | None = None
    on_ac: bool = True
    profile: str | None = None           # one of PROFILES, when power-profiles-daemon is there
    profiles: tuple[str, ...] = ()
    can_suspend: bool = False
    can_hibernate: bool = False


@dataclass(frozen=True)
class BrightnessState:
    level: float | None = None           # 0.0 to 1.0; None without a backlight


DEFAULTS = {NETWORK: NetworkState(), BLUETOOTH: BluetoothState(), AUDIO: AudioState(), POWER: PowerState(),
            BRIGHTNESS: BrightnessState()}


@dataclass(frozen=True)
class Ready:
    version: str = ""


@dataclass(frozen=True)
class BridgeError:
    message: str = ""


# --- tolerant field readers ---------------------------------------------------------------------------------

def _flag(value: object) -> bool:
    return value is True


def _text(value: object, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    # One line of printable text: control characters would break rows and logs.
    return "".join(ch if ch.isprintable() else " " for ch in value[:limit * 4]).strip()[:limit]


def _optional_text(value: object, limit: int = MAX_TEXT) -> str | None:
    text = _text(value, limit)
    return text or None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _percent(value: object) -> int | None:
    number = _number(value)
    return None if number is None else max(0, min(100, int(round(number))))


def _dicts(value: object, limit: int) -> list[dict]:
    return [item for item in value[:limit] if isinstance(item, dict)] if isinstance(value, list) else []


def _access_point(doc: dict) -> AccessPoint | None:
    ssid = doc.get("ssid")
    if not isinstance(ssid, str) or not ssid or len(ssid.encode("utf-8", "replace")) > MAX_SSID_BYTES \
            or "\0" in ssid:
        return None
    return AccessPoint(ssid, _percent(doc.get("strength")) or 0, _flag(doc.get("secured")), _flag(doc.get("known")),
                       _flag(doc.get("connected")), _flag(doc.get("connecting")))


def _network(doc: dict) -> NetworkState:
    points, seen = [], set()
    for item in _dicts(doc.get("access_points"), MAX_ACCESS_POINTS):
        point = _access_point(item)
        if point is not None and point.ssid not in seen:        # one row per SSID, the first (strongest) wins
            seen.add(point.ssid)
            points.append(point)
    connectivity = doc.get("connectivity")
    connected = doc.get("wifi_connected")
    return NetworkState(
        available=_flag(doc.get("available")),
        connectivity=connectivity if connectivity in CONNECTIVITY else "none",
        wired=_flag(doc.get("wired")), wifi_present=_flag(doc.get("wifi_present")),
        wifi_enabled=_flag(doc.get("wifi_enabled")),
        wifi_connected=connected if isinstance(connected, str) and connected and "\0" not in connected
        and len(connected.encode("utf-8", "replace")) <= MAX_SSID_BYTES else None,
        wifi_scanning=_flag(doc.get("wifi_scanning")), access_points=tuple(points), vpn=_flag(doc.get("vpn")),
        airplane=_flag(doc.get("airplane")), error=_optional_text(doc.get("error"), 400))


def _bluetooth_device(doc: dict) -> BluetoothDevice | None:
    address = doc.get("address")
    if not isinstance(address, str) or not ADDRESS_RE.fullmatch(address):
        return None
    icon = _text(doc.get("icon"), 64)
    return BluetoothDevice(address.upper(), _text(doc.get("name"), 128),
                           icon if re.fullmatch(r"[A-Za-z0-9._-]*", icon) else "", _flag(doc.get("paired")),
                           _flag(doc.get("connected")), _flag(doc.get("connecting")), _percent(doc.get("battery")))


def _bluetooth(doc: dict) -> BluetoothState:
    devices, seen = [], set()
    for item in _dicts(doc.get("devices"), MAX_DEVICES):
        device = _bluetooth_device(item)
        if device is not None and device.address not in seen:
            seen.add(device.address)
            devices.append(device)
    return BluetoothState(_flag(doc.get("available")), _flag(doc.get("enabled")), _flag(doc.get("discovering")),
                          tuple(devices))


def _audio_device(doc: object) -> AudioDevice | None:
    if not isinstance(doc, dict):
        return None
    ident = doc.get("id")
    if isinstance(ident, bool) or not isinstance(ident, int) or not 0 <= ident <= 0xFFFFFFFF:
        return None
    volume = _number(doc.get("volume"))
    return AudioDevice(ident, _text(doc.get("name"), 128), max(0.0, min(MAX_VOLUME, volume or 0.0)),
                       _flag(doc.get("muted")), _flag(doc.get("default")))


def _audio_devices(value: object) -> tuple[AudioDevice, ...]:
    devices, seen = [], set()
    for item in _dicts(value, MAX_DEVICES):
        device = _audio_device(item)
        if device is not None and device.id not in seen:
            seen.add(device.id)
            devices.append(device)
    return tuple(devices)


def _audio(doc: dict) -> AudioState:
    return AudioState(_flag(doc.get("available")), _audio_device(doc.get("output")), _audio_device(doc.get("input")),
                      _audio_devices(doc.get("outputs")), _audio_devices(doc.get("inputs")))


def _power(doc: dict) -> PowerState:
    battery = None
    raw = doc.get("battery")
    if isinstance(raw, dict) and _percent(raw.get("percent")) is not None:
        seconds = _number(raw.get("seconds_left"))
        battery = Battery(_percent(raw.get("percent")), _flag(raw.get("charging")), _flag(raw.get("full")),
                          int(seconds) if seconds is not None and 0 <= seconds <= 30 * 24 * 3600 else None)
    profiles = tuple(dict.fromkeys(p for p in doc.get("profiles", ()) if p in PROFILES)) \
        if isinstance(doc.get("profiles"), list) else ()
    profile = doc.get("profile")
    return PowerState(battery, doc.get("on_ac") is not False, profile if profile in PROFILES else None, profiles,
                      _flag(doc.get("can_suspend")), _flag(doc.get("can_hibernate")))


def _brightness(doc: dict) -> BrightnessState:
    level = _number(doc.get("level"))
    return BrightnessState(None if level is None else max(0.0, min(1.0, level)))


_PARSERS = {NETWORK: _network, BLUETOOTH: _bluetooth, AUDIO: _audio, POWER: _power, BRIGHTNESS: _brightness}


def parse_event(doc: object) -> tuple[str, object] | None:
    """(event name, state) of a decoded event object; None for an event this client does not know.

    The state is the service's dataclass for the five services, Ready for
    "ready" and BridgeError for "error". Raises ValueError when ``doc`` is
    not an event object at all.
    """
    if not isinstance(doc, dict) or not isinstance(doc.get("event"), str):
        raise ValueError("not an event object")
    name = doc["event"]
    if name in _PARSERS:
        return name, _PARSERS[name](doc)
    if name == "ready":
        return name, Ready(_text(doc.get("version"), 64))
    if name == "error":
        return name, BridgeError(_text(doc.get("message"), 400))
    return None


class Decoder:
    """Turns the bytes of the bridge's standard output into events, line by line.

    ``feed(data)`` returns the (name, state) pairs completed by ``data``.
    ``dropped`` counts the lines thrown away as malformed (too long, not
    UTF-8, not a JSON object, no "event"); ``ignored`` counts well-formed
    events with a name this client does not know.
    """

    def __init__(self, max_line: int = MAX_LINE):
        self.max_line = max_line
        self.dropped = 0
        self.ignored = 0
        self._buffer = bytearray()
        self._skipping = False            # inside an over-long line: discard up to its end

    def feed(self, data: bytes) -> list[tuple[str, object]]:
        events = []
        self._buffer += data
        while True:
            end = self._buffer.find(b"\n")
            if end < 0:
                if len(self._buffer) > self.max_line:
                    self._buffer.clear()
                    if not self._skipping:
                        self._skipping = True
                        self.dropped += 1
                return events
            line = bytes(self._buffer[:end])
            del self._buffer[:end + 1]
            if self._skipping:
                self._skipping = False    # the tail of a line that was already counted
                continue
            if len(line) > self.max_line:
                self.dropped += 1
                continue
            event = self._line(line)
            if event is not None:
                events.append(event)

    def _line(self, line: bytes) -> tuple[str, object] | None:
        line = line.strip()
        if not line:
            return None
        try:
            parsed = parse_event(json.loads(line.decode("utf-8")))
        except (ValueError, RecursionError):
            self.dropped += 1
            return None
        if parsed is None:
            self.ignored += 1
        return parsed

    def reset(self) -> None:
        """Forget a partial line (the process it came from has ended)."""
        self._buffer.clear()
        self._skipping = False


# --- commands -----------------------------------------------------------------------------------------------

def _need_flag(command: dict, key: str) -> bool:
    if not isinstance(command.get(key), bool):
        raise ValueError(f"{command['cmd']}: {key} must be true or false")
    return command[key]


def _need_ssid(command: dict) -> str:
    ssid = command.get("ssid")
    if not isinstance(ssid, str) or not ssid or "\0" in ssid or len(ssid.encode("utf-8")) > MAX_SSID_BYTES:
        raise ValueError(f"{command['cmd']}: ssid must be 1 to {MAX_SSID_BYTES} bytes")
    return ssid


def _need_number(command: dict, key: str, low: float, high: float) -> float:
    value = _number(command.get(key))
    if value is None or not low <= value <= high:
        raise ValueError(f"{command['cmd']}: {key} must be a number between {low} and {high}")
    return round(value, 4)


def _need_address(command: dict) -> str:
    address = command.get("address")
    if not isinstance(address, str) or not ADDRESS_RE.fullmatch(address):
        raise ValueError(f"{command['cmd']}: address must look like AA:BB:CC:DD:EE:FF")
    return address.upper()


def _need_id(command: dict) -> int:
    ident = command.get("id")
    if isinstance(ident, bool) or not isinstance(ident, int) or not 0 <= ident <= 0xFFFFFFFF:
        raise ValueError(f"{command['cmd']}: id must be a device id")
    return ident


def _wifi_connect(command: dict) -> dict:
    out = {"ssid": _need_ssid(command)}
    secret = command.get("passphrase")
    if secret is not None:
        if not isinstance(secret, str) or not secret or "\0" in secret \
                or len(secret.encode("utf-8")) > MAX_SECRET_BYTES:
            raise ValueError(f"wifi-connect: passphrase must be 1 to {MAX_SECRET_BYTES} bytes")
        out["passphrase"] = secret
    return out


def _refresh(command: dict) -> dict:
    if command.get("service") not in SERVICES:
        raise ValueError("refresh: unknown service")
    return {"service": command["service"]}


def _profile(command: dict) -> dict:
    if command.get("profile") not in PROFILES:
        raise ValueError("set-power-profile: unknown profile")
    return {"profile": command["profile"]}


# cmd -> the function that returns its validated fields (the bridge's `command` table).
COMMANDS = {
    "refresh": _refresh,
    "wifi-set-enabled": lambda c: {"enabled": _need_flag(c, "enabled")},
    "airplane-mode": lambda c: {"enabled": _need_flag(c, "enabled")},
    "bluetooth-set-enabled": lambda c: {"enabled": _need_flag(c, "enabled")},
    "bluetooth-discover": lambda c: {"enabled": _need_flag(c, "enabled")},
    "wifi-scan": lambda c: {},
    "wifi-disconnect": lambda c: {},
    "wifi-connect": _wifi_connect,
    "wifi-forget": lambda c: {"ssid": _need_ssid(c)},
    "bluetooth-connect": lambda c: {"address": _need_address(c)},
    "bluetooth-disconnect": lambda c: {"address": _need_address(c)},
    "bluetooth-forget": lambda c: {"address": _need_address(c)},
    "set-volume": lambda c: {"volume": _need_number(c, "volume", 0.0, MAX_VOLUME)},
    "set-input-volume": lambda c: {"volume": _need_number(c, "volume", 0.0, MAX_VOLUME)},
    "set-muted": lambda c: {"muted": _need_flag(c, "muted")},
    "set-input-muted": lambda c: {"muted": _need_flag(c, "muted")},
    "set-default-output": lambda c: {"id": _need_id(c)},
    "set-default-input": lambda c: {"id": _need_id(c)},
    "set-brightness": lambda c: {"level": _need_number(c, "level", 0.0, 1.0)},
    "set-power-profile": _profile,
}


def validated(command: object) -> dict:
    """The command with exactly the fields the bridge reads; ValueError for anything it would refuse."""
    if not isinstance(command, dict) or not isinstance(command.get("cmd"), str):
        raise ValueError("a command is a dictionary with a cmd")
    builder = COMMANDS.get(command["cmd"])
    if builder is None:
        raise ValueError(f"{command['cmd']!r} is not a command of the services bridge")
    unknown = set(command) - {"cmd", "ssid", "passphrase", "enabled", "address", "volume", "muted", "id", "level",
                              "profile", "service"}
    if unknown:
        raise ValueError(f"{command['cmd']}: unknown field {sorted(unknown)[0]!r}")
    return {"cmd": command["cmd"], **builder(command)}


def encode(command: object) -> bytes:
    """The line to write to the bridge for ``command`` (validated; ends with a newline)."""
    line = json.dumps(validated(command), ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(line) > MAX_COMMAND:
        raise ValueError("the command is too long")
    return line


def describe(command: object) -> str:
    """A command as text for a log or a test, never with a passphrase."""
    if not isinstance(command, dict):
        return "invalid command"
    parts = [str(command.get("cmd"))]
    for key, value in command.items():
        if key == "cmd":
            continue
        parts.append(f"{key}=<withheld>" if key == "passphrase" else f"{key}={value!r}")
    return " ".join(parts)


def command(cmd: str, **fields) -> dict:
    """{"cmd": cmd, **fields} after validation: build commands with this."""
    return validated({"cmd": cmd, **fields})


# --- the client's own bookkeeping -----------------------------------------------------------------------------

# What the client is doing; RUNNING is the only status in which commands are sent.
STOPPED, STARTING, RUNNING, UNAVAILABLE = "stopped", "starting", "running", "unavailable"
RESTART_DELAYS = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0)      # seconds before the 1st, 2nd, ... restart in a row
STABLE_AFTER = 30.0                                    # a bridge that ran this long starts the row again


def restart_delay(failures: int) -> float:
    """How long to wait before restart number ``failures`` (1 = the first after a run that worked)."""
    return RESTART_DELAYS[max(0, min(failures, len(RESTART_DELAYS)) - 1)]


@dataclass
class States:
    """The last state of every service; unknown until the bridge has reported it."""

    values: dict = field(default_factory=lambda: dict(DEFAULTS))
    reported: set = field(default_factory=set)

    def get(self, service: str):
        if service not in DEFAULTS:
            raise KeyError(service)
        return self.values[service]

    def apply(self, service: str, state: object) -> bool:
        """Store a reported state; True when it differs from the previous one (or is the first report)."""
        if service not in DEFAULTS or not isinstance(state, type(DEFAULTS[service])):
            raise KeyError(service)
        first = service not in self.reported
        changed = first or self.values[service] != state
        self.values[service] = state
        self.reported.add(service)
        return changed

    def reset(self) -> list[str]:
        """The bridge is gone: every service is unavailable again. Returns the services whose state changed."""
        changed = [name for name in SERVICES if self.values[name] != DEFAULTS[name] or name in self.reported]
        self.values = dict(DEFAULTS)
        self.reported.clear()
        return changed


def signal_bars(strength: int) -> str:
    """none, weak, ok, good or excellent for a Wi-Fi signal of 0 to 100 (the names of the status icons)."""
    if strength >= 80:
        return "excellent"
    if strength >= 55:
        return "good"
    if strength >= 30:
        return "ok"
    return "weak" if strength >= 5 else "none"


def with_fields(state, **changes):
    """A copy of a state with some fields replaced (tests and the fake bridge)."""
    return replace(state, **changes)
