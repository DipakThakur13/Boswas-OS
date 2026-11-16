"""The services bridge: its wire format (no Qt), the client and the fake.

The protocol tests feed the decoder realistic and hostile lines. The client
is tested against small Python stand-ins for the bridge (start, events,
commands on the pipe, a bridge that dies and is started again, stop) and,
when the built bridge exists in the checkout, against the real binary for
start, parse and stop only: in the build sandbox every service reports "not
available", and nothing is changed.

Run: QT_QPA_PLATFORM=offscreen python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures as fx  # noqa: E402,F401

from boswas_control_center import servicebridge as sb  # noqa: E402

try:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication, QWidget
    PYSIDE = True
except ImportError:
    PYSIDE = False

REAL_BRIDGE = fx.PACKAGE.parent / "boswas-desktop" / "target" / "debug" / "boswas-services-bridge"

NETWORK_LINE = {
    "event": "network", "available": True, "connectivity": "full", "wired": False, "wifi_present": True,
    "wifi_enabled": True, "wifi_connected": "Home", "wifi_scanning": False, "vpn": False, "airplane": False,
    "error": None,
    "access_points": [
        {"ssid": "Home", "strength": 82, "secured": True, "known": True, "connected": True, "connecting": False},
        {"ssid": "Café 5G", "strength": 40, "secured": True, "known": False, "connected": False,
         "connecting": False},
        {"ssid": "Open", "strength": 15, "secured": False, "known": False, "connected": False, "connecting": False}],
}
BLUETOOTH_LINE = {
    "event": "bluetooth", "available": True, "enabled": True, "discovering": False,
    "devices": [{"address": "aa:bb:cc:dd:ee:ff", "name": "Headphones", "icon": "audio-headphones", "paired": True,
                 "connected": True, "connecting": False, "battery": 80},
                {"address": "11:22:33:44:55:66", "name": "", "icon": "", "paired": False, "connected": False,
                 "connecting": False, "battery": None}],
}
AUDIO_LINE = {
    "event": "audio", "available": True,
    "output": {"id": 40, "name": "Speakers", "volume": 0.62, "muted": False, "default": True},
    "input": {"id": 51, "name": "Microphone", "volume": 0.8, "muted": True, "default": True},
    "outputs": [{"id": 40, "name": "Speakers", "volume": 0.62, "muted": False, "default": True},
                {"id": 41, "name": "HDMI", "volume": 1.2, "muted": False, "default": False}],
    "inputs": [{"id": 51, "name": "Microphone", "volume": 0.8, "muted": True, "default": True}],
}
POWER_LINE = {"event": "power", "battery": {"percent": 76, "charging": False, "full": False, "seconds_left": 12300},
              "on_ac": False, "profile": "balanced", "profiles": ["power-saver", "balanced", "performance"],
              "can_suspend": True, "can_hibernate": False}


def line(doc) -> bytes:
    return json.dumps(doc).encode("utf-8") + b"\n"


class DecoderTest(unittest.TestCase):
    def test_realistic_events(self):
        decoder = sb.Decoder()
        events = decoder.feed(line({"event": "ready", "version": "1.0.0-alpha.3"}) + line(NETWORK_LINE)
                              + line(BLUETOOTH_LINE) + line(AUDIO_LINE) + line(POWER_LINE)
                              + line({"event": "brightness", "level": 0.8})
                              + line({"event": "error", "message": "unknown \"cmd\""}))
        self.assertEqual([name for name, _state in events],
                         ["ready", "network", "bluetooth", "audio", "power", "brightness", "error"])
        states = dict(events)
        self.assertEqual(states["ready"], sb.Ready("1.0.0-alpha.3"))
        network = states["network"]
        self.assertTrue(network.available and network.wifi_enabled)
        self.assertEqual(network.wifi_connected, "Home")
        self.assertEqual([p.ssid for p in network.access_points], ["Home", "Café 5G", "Open"])
        self.assertEqual(network.access_points[0], sb.AccessPoint("Home", 82, True, True, True, False))
        self.assertIsNone(network.error)
        bluetooth = states["bluetooth"]
        self.assertEqual(bluetooth.devices[0], sb.BluetoothDevice("AA:BB:CC:DD:EE:FF", "Headphones",
                                                                  "audio-headphones", True, True, False, 80))
        self.assertIsNone(bluetooth.devices[1].battery)
        audio = states["audio"]
        self.assertEqual(audio.output, sb.AudioDevice(40, "Speakers", 0.62, False, True))
        self.assertEqual([d.id for d in audio.outputs], [40, 41])
        self.assertTrue(audio.input.muted)
        power = states["power"]
        self.assertEqual(power.battery, sb.Battery(76, False, False, 12300))
        self.assertEqual((power.profile, power.profiles), ("balanced", sb.PROFILES))
        self.assertEqual(states["brightness"], sb.BrightnessState(0.8))
        self.assertEqual(states["error"], sb.BridgeError('unknown "cmd"'))
        self.assertEqual((decoder.dropped, decoder.ignored), (0, 0))

    def test_what_the_bridge_prints_without_any_service(self):
        text = ('{"access_points":[],"airplane":false,"available":false,"connectivity":"none","error":null,'
                '"event":"network","vpn":false,"wifi_connected":null,"wifi_enabled":false,"wifi_present":false,'
                '"wifi_scanning":false,"wired":false}\n'
                '{"battery":null,"can_hibernate":false,"can_suspend":false,"event":"power","on_ac":true,'
                '"profile":null,"profiles":[]}\n'
                '{"available":false,"event":"audio","input":null,"inputs":[],"output":null,"outputs":[]}\n'
                '{"event":"brightness","level":null}\n'
                '{"available":false,"devices":[],"discovering":false,"enabled":false,"event":"bluetooth"}\n')
        events = dict(sb.Decoder().feed(text.encode()))
        for service in sb.SERVICES:
            self.assertEqual(events[service], sb.DEFAULTS[service], service)

    def test_lines_arrive_in_pieces(self):
        decoder = sb.Decoder()
        data = line(NETWORK_LINE) + line(POWER_LINE)
        events = []
        for index in range(0, len(data), 7):
            events += decoder.feed(data[index:index + 7])
        self.assertEqual([name for name, _s in events], ["network", "power"])
        self.assertEqual(decoder.feed(b""), [])
        self.assertEqual(decoder.feed(b"\n\n  \n"), [])                 # empty lines are nothing
        self.assertEqual(decoder.dropped, 0)

    def test_unknown_fields_and_events_are_ignored(self):
        decoder = sb.Decoder()
        doc = dict(NETWORK_LINE, hotspot={"enabled": True}, signal_unit="dBm")
        doc["access_points"] = [dict(doc["access_points"][0], frequency=5180, bssid="00:11:22:33:44:55")]
        events = decoder.feed(line(doc) + line({"event": "notifications", "items": []})
                              + line({"event": "tray"}))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1].access_points, (sb.AccessPoint("Home", 82, True, True, True, False),))
        self.assertEqual((decoder.dropped, decoder.ignored), (0, 2))

    def test_malformed_lines_are_dropped_and_counted(self):
        decoder = sb.Decoder()
        bad = [b"not json\n", b"{\n", b"[1,2,3]\n", b'"text"\n', b"42\n", b"null\n", b'{"available":true}\n',
               b'{"event":7}\n', b'{"event":null}\n', b"\xff\xfe\x00bad utf8\n", b'{"event":"network",}\n',
               b"[" * 5000 + b"\n"]
        events = decoder.feed(b"".join(bad) + line(POWER_LINE))
        self.assertEqual([name for name, _s in events], ["power"])      # the stream goes on after rubbish
        self.assertEqual(decoder.dropped, len(bad))

    def test_over_long_lines(self):
        decoder = sb.Decoder(max_line=1000)
        self.assertEqual(decoder.feed(b"x" * 600), [])
        self.assertEqual(decoder.feed(b"x" * 600), [])                  # over the cap without a line end: dropped
        self.assertEqual(decoder.dropped, 1)
        self.assertLess(len(decoder._buffer), 1001)
        self.assertEqual(decoder.feed(b"y" * 5000), [])                 # still the same line: memory stays bounded
        self.assertLess(len(decoder._buffer), 1001)
        events = decoder.feed(b"tail of it\n" + line({"event": "brightness", "level": 0.5}))
        self.assertEqual(events, [("brightness", sb.BrightnessState(0.5))])
        self.assertEqual(decoder.dropped, 1)
        long_line = line({"event": "error", "message": "m" * 2000})
        self.assertEqual(decoder.feed(long_line + line({"event": "ready"})), [("ready", sb.Ready(""))])
        self.assertEqual(decoder.dropped, 2)
        self.assertEqual(sb.MAX_LINE, 256 * 1024)

    def test_wrong_types_become_defaults(self):
        doc = {"event": "network", "available": "yes", "connectivity": "galactic", "wired": 1, "wifi_present": None,
               "wifi_enabled": [], "wifi_connected": 5, "wifi_scanning": {}, "vpn": "true", "airplane": 0,
               "error": ["x"], "access_points": "none"}
        self.assertEqual(sb.parse_event(doc), ("network", sb.NetworkState()))
        doc = {"event": "network", "available": True, "access_points": [
            None, 5, "x", {}, {"ssid": ""}, {"ssid": 7}, {"ssid": "a\0b"}, {"ssid": "x" * 33},
            {"ssid": "Ok", "strength": "strong", "secured": "yes"}, {"ssid": "Loud", "strength": 400},
            {"ssid": "Neg", "strength": -5}, {"ssid": "Ok", "strength": 99}, {"ssid": "NaN", "strength": float("nan")}]}
        points = sb.parse_event(doc)[1].access_points
        self.assertEqual([(p.ssid, p.strength, p.secured) for p in points],
                         [("Ok", 0, False), ("Loud", 100, False), ("Neg", 0, False), ("NaN", 0, False)])
        audio = sb.parse_event({"event": "audio", "available": True, "output": "speakers", "outputs": [
            {"id": -1}, {"id": 2 ** 40}, {"id": True}, {"id": "4"}, {"name": "no id"},
            {"id": 3, "name": 9, "volume": 99, "muted": "no"}, {"id": 3, "name": "again"},
            {"id": 4, "volume": -2}, {"id": 5, "volume": float("inf")}]})[1]
        self.assertIsNone(audio.output)
        self.assertEqual(audio.outputs, (sb.AudioDevice(3, "", 1.5, False, False), sb.AudioDevice(4, "", 0.0),
                                         sb.AudioDevice(5, "", 0.0)))
        power = sb.parse_event({"event": "power", "battery": {"percent": "full"}, "profile": "turbo",
                                "profiles": ["turbo", "balanced", "balanced", 7]})[1]
        self.assertEqual(power, sb.PowerState(None, True, None, ("balanced",)))
        power = sb.parse_event({"event": "power", "battery": {"percent": 250, "seconds_left": -5}, "on_ac": False})[1]
        self.assertEqual(power.battery, sb.Battery(100, False, False, None))
        self.assertEqual(sb.parse_event({"event": "brightness", "level": 7})[1], sb.BrightnessState(1.0))
        self.assertEqual(sb.parse_event({"event": "brightness", "level": "max"})[1], sb.BrightnessState(None))
        self.assertEqual(sb.parse_event({"event": "brightness", "level": True})[1], sb.BrightnessState(None))
        bluetooth = sb.parse_event({"event": "bluetooth", "available": True, "devices": [
            {"address": "not an address"}, {"address": "AA:BB:CC:DD:EE"}, {"address": 5}, {},
            {"address": "aa:bb:cc:dd:ee:01", "name": "x" * 500, "icon": "../../etc/passwd", "battery": 900},
            {"address": "AA:BB:CC:DD:EE:01", "name": "duplicate"}]})[1]
        self.assertEqual(len(bluetooth.devices), 1)
        self.assertEqual(bluetooth.devices[0].address, "AA:BB:CC:DD:EE:01")
        self.assertEqual(len(bluetooth.devices[0].name), 128)
        self.assertEqual(bluetooth.devices[0].icon, "")                 # not an icon name
        self.assertEqual(bluetooth.devices[0].battery, 100)
        for bad in (None, [], "event", {"event": 5}, {}):
            with self.assertRaises(ValueError):
                sb.parse_event(bad)

    def test_text_from_outside_is_one_printable_line(self):
        state = sb.parse_event({"event": "network", "available": True, "error": "bad\npass\x1b[31mword\0" + "x" * 900,
                                "access_points": [{"ssid": "Evil\nName\t!"}]})[1]
        self.assertNotIn("\n", state.error)
        self.assertNotIn("\x1b", state.error)
        self.assertLessEqual(len(state.error), 400)
        self.assertEqual(state.access_points[0].ssid, "Evil\nName\t!")   # an SSID is bytes of the air: kept as sent
        device = sb.parse_event({"event": "bluetooth", "devices": [
            {"address": "AA:BB:CC:DD:EE:01", "name": "Head\nphones‮"}]})[1].devices[0]
        self.assertEqual(device.name, "Head phones")

    def test_caps(self):
        many = [{"ssid": f"net{i}", "strength": i % 100} for i in range(1000)]
        self.assertEqual(len(sb.parse_event({"event": "network", "access_points": many})[1].access_points),
                         sb.MAX_ACCESS_POINTS)
        devices = [{"address": f"AA:BB:CC:DD:{i // 256:02X}:{i % 256:02X}"} for i in range(1000)]
        self.assertEqual(len(sb.parse_event({"event": "bluetooth", "devices": devices})[1].devices), sb.MAX_DEVICES)


class CommandTest(unittest.TestCase):
    def test_every_command_of_the_bridge(self):
        cases = [
            ({"cmd": "refresh", "service": "audio"}, b'{"cmd":"refresh","service":"audio"}\n'),
            ({"cmd": "wifi-set-enabled", "enabled": False}, b'{"cmd":"wifi-set-enabled","enabled":false}\n'),
            ({"cmd": "wifi-scan"}, b'{"cmd":"wifi-scan"}\n'),
            ({"cmd": "wifi-connect", "ssid": "Home"}, b'{"cmd":"wifi-connect","ssid":"Home"}\n'),
            ({"cmd": "wifi-connect", "ssid": "Café 5G", "passphrase": "correct horse"},
             '{"cmd":"wifi-connect","ssid":"Café 5G","passphrase":"correct horse"}\n'.encode()),
            ({"cmd": "wifi-disconnect"}, b'{"cmd":"wifi-disconnect"}\n'),
            ({"cmd": "wifi-forget", "ssid": "Home"}, b'{"cmd":"wifi-forget","ssid":"Home"}\n'),
            ({"cmd": "airplane-mode", "enabled": True}, b'{"cmd":"airplane-mode","enabled":true}\n'),
            ({"cmd": "bluetooth-set-enabled", "enabled": True}, b'{"cmd":"bluetooth-set-enabled","enabled":true}\n'),
            ({"cmd": "bluetooth-discover", "enabled": False}, b'{"cmd":"bluetooth-discover","enabled":false}\n'),
            ({"cmd": "bluetooth-connect", "address": "aa:bb:cc:dd:ee:0f"},
             b'{"cmd":"bluetooth-connect","address":"AA:BB:CC:DD:EE:0F"}\n'),
            ({"cmd": "bluetooth-disconnect", "address": "AA:BB:CC:DD:EE:FF"},
             b'{"cmd":"bluetooth-disconnect","address":"AA:BB:CC:DD:EE:FF"}\n'),
            ({"cmd": "bluetooth-forget", "address": "AA:BB:CC:DD:EE:FF"},
             b'{"cmd":"bluetooth-forget","address":"AA:BB:CC:DD:EE:FF"}\n'),
            ({"cmd": "set-volume", "volume": 0.5}, b'{"cmd":"set-volume","volume":0.5}\n'),
            ({"cmd": "set-input-volume", "volume": 1.5}, b'{"cmd":"set-input-volume","volume":1.5}\n'),
            ({"cmd": "set-muted", "muted": True}, b'{"cmd":"set-muted","muted":true}\n'),
            ({"cmd": "set-input-muted", "muted": False}, b'{"cmd":"set-input-muted","muted":false}\n'),
            ({"cmd": "set-default-output", "id": 41}, b'{"cmd":"set-default-output","id":41}\n'),
            ({"cmd": "set-default-input", "id": 0}, b'{"cmd":"set-default-input","id":0}\n'),
            ({"cmd": "set-brightness", "level": 0.25}, b'{"cmd":"set-brightness","level":0.25}\n'),
            ({"cmd": "set-power-profile", "profile": "power-saver"},
             b'{"cmd":"set-power-profile","profile":"power-saver"}\n'),
        ]
        self.assertEqual({c["cmd"] for c, _l in cases}, set(sb.COMMANDS))
        for command, expected in cases:
            self.assertEqual(sb.encode(command), expected, command)
            self.assertEqual(sb.command(command["cmd"], **{k: v for k, v in command.items() if k != "cmd"}),
                             json.loads(expected))

    def test_invalid_commands_never_reach_the_pipe(self):
        bad = [None, "wifi-scan", [], {}, {"cmd": 5}, {"cmd": "reboot"}, {"cmd": "power-off"},
               {"cmd": "notify", "text": "x"}, {"cmd": "wifi-scan", "extra": 1},
               {"cmd": "refresh"}, {"cmd": "refresh", "service": "tray"},
               {"cmd": "wifi-set-enabled"}, {"cmd": "wifi-set-enabled", "enabled": 1},
               {"cmd": "wifi-set-enabled", "enabled": "true"},
               {"cmd": "wifi-connect"}, {"cmd": "wifi-connect", "ssid": ""}, {"cmd": "wifi-connect", "ssid": "x" * 33},
               {"cmd": "wifi-connect", "ssid": "ä" * 17}, {"cmd": "wifi-connect", "ssid": "a\0b"},
               {"cmd": "wifi-connect", "ssid": 5}, {"cmd": "wifi-connect", "ssid": "Home", "passphrase": ""},
               {"cmd": "wifi-connect", "ssid": "Home", "passphrase": "x" * 257},
               {"cmd": "wifi-connect", "ssid": "Home", "passphrase": "a\0b"},
               {"cmd": "wifi-connect", "ssid": "Home", "passphrase": 12345678},
               {"cmd": "wifi-forget"}, {"cmd": "bluetooth-connect", "address": "AA:BB:CC:DD:EE"},
               {"cmd": "bluetooth-connect", "address": "AA-BB-CC-DD-EE-FF"}, {"cmd": "bluetooth-forget"},
               {"cmd": "bluetooth-connect", "address": "AA:BB:CC:DD:EE:FF\n"},
               {"cmd": "set-volume"}, {"cmd": "set-volume", "volume": 1.51}, {"cmd": "set-volume", "volume": -0.1},
               {"cmd": "set-volume", "volume": "0.5"}, {"cmd": "set-volume", "volume": float("nan")},
               {"cmd": "set-volume", "volume": True}, {"cmd": "set-muted", "muted": 0},
               {"cmd": "set-default-output", "id": -1}, {"cmd": "set-default-output", "id": 2 ** 32},
               {"cmd": "set-default-output", "id": 1.5}, {"cmd": "set-default-output", "id": True},
               {"cmd": "set-brightness", "level": 1.01}, {"cmd": "set-brightness"},
               {"cmd": "set-power-profile", "profile": "turbo"}, {"cmd": "set-power-profile"}]
        for command in bad:
            with self.assertRaises(ValueError, msg=repr(command)):
                sb.encode(command)

    def test_longest_valid_command_fits_the_bridge_limit(self):
        longest = sb.encode({"cmd": "wifi-connect", "ssid": "ä" * 16, "passphrase": "ä" * 128})
        self.assertLess(len(longest), sb.MAX_COMMAND)
        self.assertEqual(longest.count(b"\n"), 1)
        tricky = sb.encode({"cmd": "wifi-connect", "ssid": 'a"b\\c', "passphrase": 'p"\n\\'})
        self.assertEqual(tricky.count(b"\n"), 1)                        # a newline in a passphrase is escaped
        self.assertEqual(json.loads(tricky)["passphrase"], 'p"\n\\')

    def test_describe_never_shows_a_passphrase(self):
        text = sb.describe({"cmd": "wifi-connect", "ssid": "Home", "passphrase": "correct horse"})
        self.assertEqual(text, "wifi-connect ssid='Home' passphrase=<withheld>")
        self.assertNotIn("correct", text)
        self.assertEqual(sb.describe({"cmd": "set-volume", "volume": 0.5}), "set-volume volume=0.5")
        self.assertEqual(sb.describe(None), "invalid command")

    def test_restart_delays_grow_and_stop_growing(self):
        delays = [sb.restart_delay(n) for n in range(1, 10)]
        self.assertEqual(delays[:6], list(sb.RESTART_DELAYS))
        self.assertEqual(delays, sorted(delays))
        self.assertEqual(delays[-1], 30.0)
        self.assertEqual(sb.restart_delay(0), 0.5)

    def test_states(self):
        states = sb.States()
        self.assertEqual(states.get("network"), sb.NetworkState())
        self.assertTrue(states.apply("network", sb.NetworkState()))                 # the first report is news
        self.assertFalse(states.apply("network", sb.NetworkState()))
        self.assertTrue(states.apply("network", sb.NetworkState(available=True)))
        self.assertEqual(states.reset(), ["network"])
        self.assertEqual(states.reset(), [])
        with self.assertRaises(KeyError):
            states.get("tray")
        with self.assertRaises(KeyError):
            states.apply("network", sb.AudioState())

    def test_signal_bars(self):
        self.assertEqual([sb.signal_bars(s) for s in (0, 4, 5, 29, 30, 54, 55, 79, 80, 100)],
                         ["none", "none", "weak", "weak", "ok", "ok", "good", "good", "excellent", "excellent"])


FAKE_BRIDGE = r'''#!/usr/bin/python3 -IB
import json, os, sys
mode = os.environ.get("FAKE_BRIDGE_MODE", "")
log = os.environ.get("FAKE_BRIDGE_LOG", "")
def out(doc):
    sys.stdout.write(json.dumps(doc) + "\n"); sys.stdout.flush()
def note(text):
    if log:
        with open(log, "a") as handle:
            handle.write(text + "\n")
note("start " + " ".join(sys.argv[1:]))
out({"event": "ready", "version": "9.9"})
out({"event": "network", "available": True, "wifi_present": True, "wifi_enabled": True, "access_points": []})
sys.stdout.write("this line is rubbish\n"); sys.stdout.flush()
out({"event": "brightness", "level": 0.5})
if mode == "die":
    sys.exit(3)
for line in sys.stdin:
    line = line.strip()
    note("command " + line)
    command = json.loads(line)
    if command["cmd"] == "wifi-set-enabled":
        out({"event": "network", "available": True, "wifi_present": True, "wifi_enabled": command["enabled"]})
    elif command["cmd"] == "set-brightness":
        out({"event": "brightness", "level": command["level"]})
    elif command["cmd"] == "wifi-scan" and mode == "crash-on-scan":
        sys.exit(1)
note("end of input")
'''


@unittest.skipUnless(PYSIDE, "PySide6 is not installed")
class ClientTest(unittest.TestCase):
    def setUp(self):
        from boswas_control_center.ui import bridgeclient
        self.bc = bridgeclient
        self.app = QApplication.instance() or QApplication(["test"])
        self.tmp = tempfile.TemporaryDirectory()
        self.program = Path(self.tmp.name) / "fake-bridge"
        self.program.write_text(FAKE_BRIDGE, encoding="utf-8")
        self.program.chmod(self.program.stat().st_mode | stat.S_IXUSR)
        self.log = Path(self.tmp.name) / "log"
        os.environ["FAKE_BRIDGE_LOG"] = str(self.log)
        os.environ.pop("FAKE_BRIDGE_MODE", None)
        self.clients: list = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for client in self.clients:
            client.stop()
        os.environ.pop("FAKE_BRIDGE_LOG", None)
        os.environ.pop("FAKE_BRIDGE_MODE", None)
        QCoreApplication.processEvents()
        self.tmp.cleanup()

    def client(self, program=None):
        client = self.bc.BridgeClient(program=str(program or self.program))
        self.clients.append(client)
        return client

    @staticmethod
    def wait(condition, timeout=10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            QCoreApplication.processEvents()
            if condition():
                return True
            time.sleep(0.005)
        return bool(condition())

    def logged(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_start_events_commands_stop(self):
        client = self.client()
        changes, statuses, networks = [], [], []
        client.changed.connect(changes.append)
        client.status_changed.connect(statuses.append)
        client.network_changed.connect(networks.append)
        self.assertEqual(client.status(), sb.STOPPED)
        self.assertFalse(client.send(sb.command("wifi-scan")))          # not running: nothing is written
        client.start()
        client.start()                                                  # lazily, once
        self.assertTrue(self.wait(lambda: "brightness" in changes))
        self.assertEqual(client.status(), sb.RUNNING)
        self.assertEqual(statuses, [sb.STARTING, sb.RUNNING])
        self.assertEqual(client.version, "9.9")
        self.assertTrue(client.state("network").wifi_enabled)
        self.assertEqual(client.state("brightness"), sb.BrightnessState(0.5))
        self.assertEqual(client.state("audio"), sb.AudioState())         # not reported: not available
        self.assertEqual(client.decoder.dropped, 1)                     # the rubbish line, and the stream went on
        self.assertIsInstance(networks[0], sb.NetworkState)
        self.assertTrue(client.send(sb.command("wifi-set-enabled", enabled=False)))
        self.assertTrue(self.wait(lambda: not client.state("network").wifi_enabled))
        self.assertTrue(client.send(sb.command("set-brightness", level=0.25)))
        self.assertTrue(self.wait(lambda: client.state("brightness").level == 0.25))
        with self.assertRaises(ValueError):
            client.send({"cmd": "reboot"})
        self.assertEqual(self.logged().count("start "), 1)               # one process, started without arguments
        client.stop()
        self.assertEqual(client.status(), sb.STOPPED)
        self.assertTrue(self.wait(lambda: "end of input" in self.logged(), 5))    # it ended because stdin closed
        self.assertEqual(client.state("network"), sb.NetworkState())    # nothing is known any more
        self.assertFalse(client.send(sb.command("wifi-scan")))
        self.assertEqual(client.restarts, 0)
        client.stop()                                                   # twice is fine

    def test_a_passphrase_goes_to_the_pipe_only(self):
        client = self.client()
        client.start()
        self.assertTrue(self.wait(lambda: client.status() == sb.RUNNING))
        secret = "correct horse battery staple"
        self.assertTrue(client.send(sb.command("wifi-connect", ssid="Home", passphrase=secret)))
        self.assertTrue(self.wait(lambda: any("wifi-connect" in entry for entry in self.logged())))
        # The stand-in logged what arrived on its standard input; its command line and environment had none of it.
        self.assertIn(secret, "\n".join(self.logged()))
        self.assertEqual([entry for entry in self.logged() if entry.startswith("start")], ["start "])
        self.assertNotIn(secret, " ".join(os.environ.values()))
        self.assertNotIn(secret, repr(vars(client)))                    # and the client keeps no copy of a command

    def test_a_bridge_that_dies_is_restarted_with_back_off(self):
        os.environ["FAKE_BRIDGE_MODE"] = "crash-on-scan"
        client = self.client()
        statuses = []
        client.status_changed.connect(statuses.append)
        client.start()
        self.assertTrue(self.wait(lambda: client.state("network").available))
        client.send(sb.command("wifi-scan"))                            # the stand-in exits
        self.assertTrue(self.wait(lambda: client.status() == sb.UNAVAILABLE))
        self.assertEqual(client.problem(), self.bc.STOPPED_WORKING)
        self.assertEqual(client.state("network"), sb.NetworkState())    # unavailable meanwhile
        self.assertFalse(client.send(sb.command("wifi-scan")))
        self.assertTrue(client._timer.isActive())
        self.assertEqual(client._timer.interval(), 500)                 # the first restart comes quickly
        self.assertTrue(self.wait(lambda: client.status() == sb.RUNNING and client.state("network").available))
        self.assertEqual(client.restarts, 1)
        self.assertEqual(self.logged().count("start "), 2)
        client.send(sb.command("wifi-scan"))                            # again: the pause grows
        self.assertTrue(self.wait(lambda: client.status() == sb.UNAVAILABLE))
        self.assertEqual(client._timer.interval(), 1000)
        client.stop()                                                   # stopping cancels the restart
        self.assertFalse(client._timer.isActive())
        time.sleep(0.05)
        QCoreApplication.processEvents()
        self.assertEqual(self.logged().count("start "), 2)

    def test_a_bridge_that_ends_at_once_does_not_spin(self):
        os.environ["FAKE_BRIDGE_MODE"] = "die"
        client = self.client()
        client.start()
        self.assertTrue(self.wait(lambda: client.restarts >= 2, 15))
        self.assertGreaterEqual(client._timer.interval(), 1000)
        self.assertLessEqual(self.logged().count("start "), 3)
        self.assertEqual(client.status(), sb.UNAVAILABLE)

    def test_missing_program(self):
        client = self.client(Path(self.tmp.name) / "not-installed")
        client.start()
        self.assertTrue(self.wait(lambda: client.status() == sb.UNAVAILABLE))
        self.assertEqual(client.problem(), self.bc.NOT_INSTALLED)
        self.assertFalse(client._timer.isActive())                      # not retried: it would not help
        client.start()
        QCoreApplication.processEvents()
        self.assertEqual(client.status(), sb.UNAVAILABLE)
        self.assertFalse(client.send(sb.command("wifi-scan")))

    @unittest.skipUnless(REAL_BRIDGE.is_file(), "the services bridge is not built in this checkout")
    def test_real_bridge_start_parse_stop(self):
        client = self.client(REAL_BRIDGE)
        seen = []
        client.changed.connect(seen.append)
        client.start()
        self.assertTrue(self.wait(lambda: set(seen) >= set(sb.SERVICES), 20), seen)
        self.assertEqual(client.status(), sb.RUNNING)
        self.assertTrue(client.version)
        self.assertEqual(client.decoder.dropped, 0)                     # every line it printed was understood
        for service in sb.SERVICES:
            self.assertIsInstance(client.state(service), type(sb.DEFAULTS[service]))
        self.assertTrue(client.refresh("power"))                        # a read-only command it accepts
        errors = []
        client.error_received.connect(errors.append)
        process = client._process
        client.stop()
        self.assertEqual(errors, [])
        self.assertEqual(process.state(), process.ProcessState.NotRunning)   # it ended when its input closed
        self.assertEqual(client.status(), sb.STOPPED)

    def test_fake_bridge_has_the_same_interface(self):
        fake = self.bc.FakeBridge(states={sb.NETWORK: sb.NetworkState(available=True)})
        public = lambda cls: {name for name in dir(cls) if not name.startswith("_")}      # noqa: E731
        self.assertLessEqual(public(self.bc.BridgeClient) - {"program", "decoder", "version", "restarts"},
                             public(self.bc.FakeBridge) | {"program", "decoder", "version", "restarts"})
        seen = []
        fake.audio_changed.connect(seen.append)
        fake.changed.connect(seen.append)
        self.assertTrue(fake.state("network").available)
        self.assertFalse(fake.send(sb.command("wifi-scan")))            # not started yet
        fake.start()
        self.assertEqual(fake.status(), sb.RUNNING)
        fake.set_state(sb.AUDIO, available=True)
        self.assertEqual(seen, [sb.AudioState(available=True), "audio"])
        fake.set_state(sb.AUDIO, available=True)                         # no change, no signal
        self.assertEqual(len(seen), 2)
        self.assertTrue(fake.send({"cmd": "wifi-scan"}))
        self.assertTrue(fake.refresh("audio"))
        self.assertEqual(fake.commands(), [{"cmd": "wifi-scan"}])
        with self.assertRaises(ValueError):
            fake.send({"cmd": "format-disk"})
        fake.fail()
        self.assertEqual(fake.status(), sb.UNAVAILABLE)
        self.assertEqual(fake.state("network"), sb.NetworkState())
        missing = self.bc.FakeBridge(running=False)
        missing.start()
        self.assertEqual((missing.status(), missing.problem()), (sb.UNAVAILABLE, self.bc.NOT_INSTALLED))

    def test_one_bridge_per_window(self):
        saved = self.bc.factory
        self.bc.factory = self.bc.FakeBridge
        try:
            window, other = QWidget(), QWidget()
            child = QWidget(window)
            bridge = self.bc.for_widget(child)
            self.assertIs(self.bc.for_widget(window), bridge)
            self.assertIsNot(self.bc.for_widget(other), bridge)
            self.bc.stop_for(window)
            self.assertEqual(bridge.stopped, 1)
            self.bc.stop_for(window)                                    # nothing left to stop
            self.assertIsNot(self.bc.for_widget(window), bridge)        # a new one after that
        finally:
            self.bc.factory = saved


if __name__ == "__main__":
    unittest.main()
