"""The weather on the lock screen without Qt: the summary, the setting, ``--update-lock-screen``, the units.

The clock stands still at 14:20 in Berlin (``NOW``); the network is ``FakeNetwork``, which records every request.
"""

from __future__ import annotations

import configparser
import datetime as dt
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from weather_fixtures import NOW, FakeNetwork, place

from boswas_apps.weather import APP_ID, __main__ as launcher, lockscreen, model, places, service, settings
from boswas_apps.weather.places import PlaceList
from boswas_apps.weather.service import ForecastCache, NetworkError, WeatherService
from boswas_apps.weather.settings import Settings

PACKAGE = Path(__file__).resolve().parents[2]
REPOSITORY = PACKAGE.parents[1]
UTC = dt.timezone.utc
BERLIN_MIDNIGHT = dt.datetime(2026, 11, 21, 23, 0, tzinfo=UTC).timestamp()      # 22 Nov 00:00 in Berlin


def fetched_berlin(at: float = NOW):
    return WeatherService(fetch=FakeNetwork(), clock=lambda: at).forecast(place("berlin"))


class Home(unittest.TestCase):
    """A temporary home: XDG directories under it, and the compositor's three files (none yet)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.environ = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "config"),
                        "XDG_STATE_HOME": str(self.home / "state"), "XDG_CACHE_HOME": str(self.home / "cache"),
                        "LANG": "en_GB.UTF-8"}
        self.config = self.home / "config" / APP_ID
        self.summary = self.home / "state" / "boswas" / "weather.json"
        self.system = [str(self.home / "defaults.toml"), str(self.home / "etc.toml")]
        self.files = [*self.system, str(self.home / "config" / "boswas" / "compositor.toml")]
        self.network = FakeNetwork()
        self.log = io.StringIO()

    def save_places(self, *names: str, home: str | None = None) -> None:
        places.save(self.config / places.FILE_NAME, PlaceList([place(name) for name in names],
                                                               place(home).key if home else None))

    def update(self, clock=lambda: NOW) -> int:
        return lockscreen.update(self.environ, fetch=self.network, clock=clock, files=self.files,
                                 locale_answer=lambda: False, log=self.log)

    def read(self) -> dict:
        return json.loads(self.summary.read_text(encoding="utf-8"))

    def write_config(self, index: int, text: str) -> None:
        path = Path(self.files[index])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class SummaryTest(unittest.TestCase):
    def test_the_words_and_icons_are_chosen_here(self):
        summary = lockscreen.build_summary(place("berlin"), fetched_berlin(), Settings(), False, NOW, UTC)
        self.assertEqual({key: summary[key] for key in summary if key != "hours"}, {
            "version": 1, "place": "Berlin", "fetched": int(NOW), "unit": "c", "temperature": "8°C",
            "condition": "Partly cloudy", "icon": "weather-few-clouds-symbolic", "updated": "Updated at 13:20",
            "feels_like": "Feels like 5°", "high_low": "High 8° · Low 2°", "today_until": int(BERLIN_MIDNIGHT)})
        hours = summary["hours"]
        self.assertEqual(len(hours), lockscreen.HOURS)
        # From the hour Berlin is in (14:00, 13:00 UTC) on, every hour once, in the place's own time.
        self.assertEqual(hours[0], {"at": int(dt.datetime(2026, 11, 21, 13, tzinfo=UTC).timestamp()), "time": "14:00",
                                    "icon": "weather-few-clouds-symbolic", "temperature": "8°"})
        self.assertEqual([hour["at"] - hours[0]["at"] for hour in hours], [n * 3600 for n in range(12)])
        self.assertEqual(hours[2]["icon"], "weather-few-clouds-night-symbolic", "after sunset, the night icon")
        self.assertEqual(hours[10]["time"], "00:00")

    def test_units_and_the_twelve_hour_clock(self):
        chosen = Settings(temperature="f", time_format="12")
        summary = lockscreen.build_summary(place("berlin"), fetched_berlin(), chosen, True, NOW, UTC)
        self.assertEqual((summary["unit"], summary["temperature"], summary["feels_like"], summary["high_low"]),
                         ("f", "46°F", "Feels like 40°", "High 47° · Low 36°"))
        self.assertEqual(summary["updated"], "Updated at 1:20 PM")
        self.assertEqual([hour["time"] for hour in summary["hours"][:3]], ["2 PM", "3 PM", "4 PM"])
        self.assertEqual(summary["hours"][10]["time"], "12 AM")

    def test_night_at_the_other_end_of_the_world(self):
        result = WeatherService(fetch=FakeNetwork(), clock=lambda: NOW).forecast(place("sydney"))
        summary = lockscreen.build_summary(place("sydney"), result, Settings(), False, NOW, UTC)
        self.assertEqual((summary["place"], summary["condition"], summary["icon"]),
                         ("Sydney", "Clear", "weather-clear-night-symbolic"))
        self.assertEqual((summary["temperature"], summary["feels_like"]), ("20°C", "Feels like 20°"))

    def test_a_summary_written_later_starts_with_the_hour_then(self):
        later = NOW + 2 * 3600
        summary = lockscreen.build_summary(place("berlin"), fetched_berlin(), Settings(), False, later, UTC)
        self.assertEqual(summary["fetched"], int(NOW), "when the forecast was fetched, not when it was written")
        self.assertEqual(summary["hours"][0]["time"], "16:00")

    def test_what_the_forecast_lacks_is_left_out(self):
        result = fetched_berlin()
        forecast = result.forecast
        bare = model.Forecast(forecast.timezone, forecast.utc_offset,
                              model.Current(temperature=21.4, code=None), (), ())
        summary = lockscreen.build_summary(place("berlin"), service.Result(bare, NOW, False), Settings(), False,
                                           NOW, UTC)
        self.assertEqual((summary["temperature"], summary["condition"], summary["icon"], summary["hours"]),
                         ("21°C", "Unknown conditions", "weather-clouds-symbolic", []))
        for key in ("feels_like", "high_low", "today_until"):
            self.assertNotIn(key, summary)
        nothing_now = model.Forecast(forecast.timezone, forecast.utc_offset, None, (), forecast.days)
        self.assertIsNone(lockscreen.build_summary(place("berlin"), service.Result(nothing_now, NOW, False),
                                                   Settings(), False, NOW, UTC))

    def test_the_summary_keeps_to_what_the_lock_screen_reads(self):
        """The rules of boswas-shell's weather.rs: anything else and the lock screen shows no card."""
        icon = re.compile(r"weather-[a-z-]+")
        for name, chosen, twelve in (("berlin", Settings(), False), ("sydney", Settings.imperial(), True)):
            result = WeatherService(fetch=FakeNetwork(), clock=lambda: NOW).forecast(place(name))
            summary = lockscreen.build_summary(place(name), result, chosen, twelve, NOW)
            data = lockscreen.encode(summary)
            self.assertLessEqual(len(data), lockscreen.MAX_BYTES)
            self.assertLess(len(data), 4096, "a few KB")
            self.assertEqual(summary["version"], 1)
            self.assertIn(summary["unit"], ("c", "f"))
            for key, limit in (("place", 100), ("temperature", 12), ("condition", 64), ("updated", 64),
                               ("feels_like", 64), ("high_low", 64)):
                value = summary[key]
                self.assertTrue(0 < len(value) <= limit and value.strip() and value.isprintable(), (key, value))
            for moment in (summary["fetched"], summary["today_until"], *(hour["at"] for hour in summary["hours"])):
                self.assertTrue(isinstance(moment, int) and 946684800 <= moment <= 4102444800)
            self.assertLessEqual(len(summary["hours"]), 48)
            for hour in summary["hours"]:
                self.assertEqual(set(hour), {"at", "time", "icon", "temperature"})
                self.assertTrue(0 < len(hour["time"]) <= 12 and 0 < len(hour["temperature"]) <= 12)
            for name_of_icon in (summary["icon"], *(hour["icon"] for hour in summary["hours"])):
                self.assertTrue(icon.fullmatch(name_of_icon) and len(name_of_icon) <= 64, name_of_icon)
                self.assertTrue((REPOSITORY / "desktop/icons/Boswas-Prism/scalable/actions" / f"{name_of_icon}.svg")
                                .is_file() or not (REPOSITORY / "desktop/icons").is_dir(), name_of_icon)
            self.assertEqual(json.loads(data.decode("utf-8")), summary)

    def test_every_condition_icon_is_one_the_lock_screen_accepts(self):
        for name in model.CONDITION_ICONS:
            self.assertRegex(name, r"^weather-[a-z-]+$")

    def test_an_oversized_summary_is_refused(self):
        with self.assertRaises(ValueError):
            lockscreen.encode({"version": 1, "place": "x" * (lockscreen.MAX_BYTES + 1)})
        with self.assertRaises(ValueError):
            lockscreen.encode({"version": 1, "fetched": float("nan")})


class WriteTest(Home):
    def test_written_in_one_step_and_private(self):
        summary = lockscreen.build_summary(place("berlin"), fetched_berlin(), Settings(), False, NOW)
        lockscreen.write_summary(self.summary, summary)
        self.assertEqual(self.read(), summary)
        self.assertEqual(stat.S_IMODE(self.summary.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.summary.parent.stat().st_mode), 0o700, "a directory it made")
        self.assertEqual([path.name for path in self.summary.parent.iterdir()], ["weather.json"], "no leftovers")
        # A write that fails half way leaves the previous summary as it was.
        with mock.patch("os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            lockscreen.write_summary(self.summary, dict(summary, place="Elsewhere"))
        self.assertEqual(self.read()["place"], "Berlin")
        self.assertEqual([path.name for path in self.summary.parent.iterdir()], ["weather.json"])

    def test_a_symbolic_link_is_replaced_not_followed(self):
        target = self.home / "elsewhere.json"
        target.write_text("keep", encoding="utf-8")
        self.summary.parent.mkdir(parents=True)
        self.summary.symlink_to(target)
        lockscreen.write_summary(self.summary, {"version": 1, "place": "Berlin"})
        self.assertFalse(self.summary.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_where_the_summary_goes(self):
        self.assertEqual(lockscreen.summary_path(self.environ), self.summary)
        self.assertEqual(lockscreen.summary_path({"HOME": "/home/asha", "XDG_STATE_HOME": "relative"}),
                         Path("/home/asha/.local/state/boswas/weather.json"))
        self.assertIsNone(lockscreen.summary_path({}))
        self.assertEqual(lockscreen.app_config_dir({"HOME": "/home/asha"}), Path("/home/asha/.config", APP_ID))
        self.assertEqual(lockscreen.app_cache_dir({"HOME": "/h", "XDG_CACHE_HOME": "/c"}), Path("/c", APP_ID))

    def test_sync_follows_the_window(self):
        result = fetched_berlin()
        berlin = place("berlin")
        self.assertTrue(lockscreen.sync(self.summary, berlin, result, Settings(), False, NOW, shown=True))
        self.assertEqual(self.read()["place"], "Berlin")
        for args, shown in (((berlin, None), True), ((None, result), True), ((berlin, result), False)):
            lockscreen.sync(self.summary, berlin, result, Settings(), False, NOW, shown=True)
            self.assertTrue(self.summary.exists())
            self.assertFalse(lockscreen.sync(self.summary, *args, Settings(), False, NOW, shown=shown))
            self.assertFalse(self.summary.exists(), (args, shown))
        self.assertFalse(lockscreen.sync(None, berlin, result, Settings(), False, NOW, shown=True))


class SettingTest(Home):
    def test_on_unless_a_file_says_otherwise(self):
        self.assertTrue(lockscreen.show_weather(self.files))
        self.write_config(0, "[lock]\nidle_seconds = 600\nshow_weather = true\n")
        self.write_config(1, "[lock]\nshow_weather = false\n")
        self.assertFalse(lockscreen.show_weather(self.files), "the administrator's default")
        self.write_config(2, "[lock]\nshow_weather = true\n")
        self.assertTrue(lockscreen.show_weather(self.files), "the user's choice wins")
        self.write_config(2, "[appearance]\nscheme = \"light\"\n")
        self.assertFalse(lockscreen.show_weather(self.files), "a user file without the key changes nothing")

    def test_files_that_say_nothing(self):
        self.write_config(1, "[lock]\nshow_weather = false\n")
        for text in ('[lock]\nshow_weather = "yes"\n', "[lock\nshow_weather = true\n", "lock = 3\n",
                     "[lock]\nshow_weather = 1\n"):
            self.write_config(2, text)
            self.assertFalse(lockscreen.show_weather(self.files), text)
        Path(self.files[2]).write_bytes(b"[lock]\nshow_weather = true\n# \xff\xfe\n")
        self.assertFalse(lockscreen.show_weather(self.files), "not UTF-8")
        Path(self.files[2]).write_bytes(b"[lock]\nshow_weather = true\n" + b"#" * lockscreen.MAX_CONFIG_BYTES)
        self.assertFalse(lockscreen.show_weather(self.files), "too large: ignored, as the compositor does")
        Path(self.files[2]).unlink()
        Path(self.files[2]).mkdir()
        self.assertFalse(lockscreen.show_weather(self.files))

    def test_the_same_files_as_the_compositor(self):
        files = lockscreen.compositor_files({"HOME": "/home/asha"})
        self.assertEqual(files, ["/usr/share/boswas/compositor/compositor.toml", "/etc/boswas/compositor.toml",
                                 "/home/asha/.config/boswas/compositor.toml"])
        self.assertEqual(lockscreen.compositor_files({"XDG_CONFIG_HOME": "/c"})[2], "/c/boswas/compositor.toml")
        config_rs = REPOSITORY / "packages/boswas-desktop/crates/boswas-compositor/src/config.rs"
        if config_rs.is_file():
            source = config_rs.read_text(encoding="utf-8")
            for path in lockscreen.SYSTEM_FILES:
                self.assertIn(f'"{path}"', source)
            self.assertIn("pub show_weather: bool", source)


class LocaleTest(unittest.TestCase):
    def test_twelve_hour_formats(self):
        for text, twelve in (("%r", True), ("%I:%M:%S %p", True), ("%l:%M %p", True), ("%H:%M:%S", False),
                             ("%T", False), ("", False)):
            self.assertEqual(lockscreen.twelve_hour_format(text), twelve, text)
        self.assertIsInstance(lockscreen.locale_twelve_hour(), bool)
        self.assertTrue(lockscreen.twelve_hour(Settings(time_format="12"), lambda: False))
        self.assertFalse(lockscreen.twelve_hour(Settings(time_format="24"), lambda: True))
        self.assertTrue(lockscreen.twelve_hour(Settings(time_format="locale"), lambda: True))

    def test_the_units_of_the_united_states(self):
        for environ, imperial in (({"LANG": "en_US.UTF-8"}, True), ({"LANG": "en_GB.UTF-8"}, False),
                                  ({"LANG": "de_DE.UTF-8", "LC_MEASUREMENT": "en_US.UTF-8"}, True),
                                  ({"LC_ALL": "fr_FR.UTF-8", "LC_MEASUREMENT": "en_US.UTF-8"}, False),
                                  ({"LANG": "my_MM"}, True), ({"LANG": "C.UTF-8"}, False), ({}, False),
                                  ({"LANG": "sr_RS@latin"}, False)):
            self.assertEqual(lockscreen.locale_imperial(environ), imperial, environ)

    def test_the_lock_screens_place(self):
        berlin, sydney = place("berlin"), place("sydney")
        self.assertIsNone(lockscreen.lock_place(PlaceList()))
        self.assertEqual(lockscreen.lock_place(PlaceList([berlin, sydney])), berlin, "no home: the first")
        self.assertEqual(lockscreen.lock_place(PlaceList([berlin, sydney], sydney.key)), sydney)


class UpdateTest(Home):
    def test_without_a_place_nothing_is_asked(self):
        self.assertEqual(self.update(), 0)
        self.assertEqual(self.network.urls, [])
        self.assertFalse(self.summary.exists())
        self.assertFalse(self.config.exists(), "Weather's directories are not made by the update")
        # A summary of places that are all gone is removed, still without a request.
        self.summary.parent.mkdir(parents=True)
        self.summary.write_text("{}", encoding="utf-8")
        self.save_places()
        self.assertEqual(self.update(), 0)
        self.assertFalse(self.summary.exists())
        self.assertEqual(self.network.urls, [])

    def test_the_home_place_is_fetched_and_written(self):
        self.save_places("sydney", "berlin", home="berlin")
        self.assertEqual(self.update(), 0)
        self.assertEqual(self.network.count("forecast"), 1)
        self.assertEqual(self.network.count("search"), 0)
        self.assertIn("latitude=52.5244", self.network.urls[0])
        summary = self.read()
        self.assertEqual((summary["place"], summary["temperature"], summary["fetched"]), ("Berlin", "8°C", int(NOW)))
        self.assertEqual(stat.S_IMODE(self.summary.stat().st_mode), 0o600)
        # Weather's cache got the forecast too, so the window opens with it.
        cached = ForecastCache(self.home / "cache" / APP_ID).read(place("berlin"), NOW)
        self.assertEqual(cached.fetched, NOW)
        # Five minutes later the cached forecast is young enough: no request.
        self.assertEqual(self.update(clock=lambda: NOW + 300), 0)
        self.assertEqual(self.network.count("forecast"), 1)
        self.assertEqual(self.read()["fetched"], int(NOW))
        self.assertEqual(self.update(clock=lambda: NOW + 1800), 0)
        self.assertEqual(self.network.count("forecast"), 2)
        self.assertEqual(self.log.getvalue(), "")

    def test_without_a_home_the_first_place(self):
        self.save_places("sydney", "berlin")
        self.assertEqual(self.update(), 0)
        self.assertEqual(self.read()["place"], "Sydney")

    def test_the_users_units_and_clock(self):
        self.save_places("berlin")
        settings.save(self.config / settings.FILE_NAME, Settings(temperature="f", time_format="12"))
        self.update()
        self.assertEqual((self.read()["temperature"], self.read()["hours"][0]["time"]), ("46°F", "2 PM"))
        # Without a settings file the units follow the locale, as the window's first start does.
        (self.config / settings.FILE_NAME).unlink()
        self.environ["LANG"] = "en_US.UTF-8"
        self.update(clock=lambda: NOW + 3600)
        self.assertEqual(self.read()["temperature"], "46°F")
        self.environ["LANG"] = "de_DE.UTF-8"
        self.update(clock=lambda: NOW + 7200)
        self.assertEqual(self.read()["temperature"], "8°C")

    def test_the_setting_off_removes_the_summary_and_asks_nothing(self):
        self.save_places("berlin")
        self.update()
        self.assertTrue(self.summary.exists())
        self.write_config(2, "[lock]\nshow_weather = false\n")
        self.assertEqual(self.update(clock=lambda: NOW + 3600), 0)
        self.assertFalse(self.summary.exists())
        self.assertEqual(self.network.count("forecast"), 1)
        self.write_config(2, "[lock]\nshow_weather = true\n")
        self.update(clock=lambda: NOW + 3600)
        self.assertTrue(self.summary.exists())

    def test_offline_keeps_the_last_summary(self):
        self.save_places("berlin")
        self.update()
        before = self.summary.read_bytes()
        self.network.error = NetworkError("Check your connection, then try again.")
        self.assertEqual(self.update(clock=lambda: NOW + 3600), 0)
        self.assertEqual(self.summary.read_bytes(), before)
        self.assertEqual(self.log.getvalue(), "", "quietly")
        # An answer that cannot be used is no better.
        self.network.error = None
        self.network.overrides["forecast"] = b"<html>busy</html>"
        self.assertEqual(self.update(clock=lambda: NOW + 3600), 0)
        self.assertEqual(self.summary.read_bytes(), before)

    def test_a_damaged_places_file_is_left_to_the_window(self):
        self.config.mkdir(parents=True)
        (self.config / places.FILE_NAME).write_text("{not json", encoding="utf-8")
        self.assertEqual(self.update(), 0)
        self.assertEqual(self.network.urls, [])
        self.assertEqual((self.config / places.FILE_NAME).read_text(encoding="utf-8"), "{not json",
                         "not set aside: the window does that, and tells the user")

    def test_a_summary_that_cannot_be_written(self):
        self.save_places("berlin")
        (self.home / "state").mkdir()
        (self.home / "state" / "boswas").write_text("a file where the directory should be", encoding="utf-8")
        self.assertEqual(self.update(), 1)
        self.assertEqual(len(self.log.getvalue().splitlines()), 1)
        self.assertIn("could not be saved", self.log.getvalue())


class LauncherTest(Home):
    def test_never_as_root(self):
        stderr = io.StringIO()
        with mock.patch("os.geteuid", return_value=0), mock.patch("sys.stderr", stderr), \
                mock.patch.object(lockscreen, "update") as update:
            self.assertEqual(launcher.main(["boswas-weather", "--update-lock-screen"]), 4)
        update.assert_not_called()
        self.assertIn("never as root", stderr.getvalue())
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            launcher.main(["boswas-weather", "--help"])
        self.assertIn("--update-lock-screen", out.getvalue())

    def test_the_update_imports_no_qt(self):
        """The launcher's path as the timer runs it, in a fresh interpreter: no PySide6, no boswas_fluent."""
        self.save_places("berlin")
        program = (
            "import json, os, sys\n"
            "os.geteuid = lambda: 1000\n"
            "from boswas_apps.weather import service\n"
            f"answer = open({str(PACKAGE / 'tests/weather/data/forecast-berlin.json')!r}, 'rb').read()\n"
            "service.http_get = lambda url, **kw: answer\n"
            "from boswas_apps.weather.__main__ import main\n"
            "status = main(['boswas-weather', '--update-lock-screen'])\n"
            "loaded = sorted(m for m in sys.modules if m.split('.')[0] in ('PySide6', 'shiboken6', 'boswas_fluent'))\n"
            "print(json.dumps({'status': status, 'loaded': loaded}))\n")
        environ = {**os.environ, **self.environ, "PYTHONPATH": str(PACKAGE)}
        environ.pop("QT_QPA_PLATFORM", None)
        done = subprocess.run([sys.executable, "-B", "-c", program], env=environ, capture_output=True, text=True,
                              timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(report, {"status": 0, "loaded": []})
        self.assertEqual(self.read()["place"], "Berlin")


class UnitsTest(unittest.TestCase):
    """The systemd user units: started with the Boswas session, every half hour, confined."""

    def unit(self, name: str) -> configparser.ConfigParser:
        parser = configparser.ConfigParser(strict=True, interpolation=None)
        parser.optionxform = str                        # systemd keys are case-sensitive
        parser.read_string((PACKAGE / "systemd" / name).read_text(encoding="utf-8"))
        return parser

    def test_the_timer(self):
        timer = self.unit("boswas-weather-update.timer")
        self.assertEqual(timer["Install"]["WantedBy"], "boswas-session.target")
        self.assertEqual(timer["Unit"]["PartOf"], "boswas-session.target", "it stops with the session")
        section = timer["Timer"]
        self.assertEqual((section["OnActiveSec"], section["OnUnitActiveSec"]), ("1min", "30min"))
        self.assertIn("RandomizedDelaySec", section)
        self.assertEqual(section["Unit"], "boswas-weather-update.service")
        self.assertNotIn("OnCalendar", section)
        self.assertNotEqual(section.get("Persistent", "no"), "yes", "no catching up on missed runs")
        session_target = REPOSITORY / "packages/boswas-desktop/session/boswas-session.target"
        if session_target.parent.is_dir():
            self.assertTrue(session_target.is_file(), "the target the timer is wanted by exists")

    def test_the_service_is_confined(self):
        service_unit = self.unit("boswas-weather-update.service")
        self.assertNotIn("Install", service_unit, "started by the timer only")
        section = service_unit["Service"]
        self.assertEqual(section["Type"], "oneshot")
        self.assertEqual(section["ExecStart"], "/usr/bin/boswas-weather --update-lock-screen")
        for key, value in (("NoNewPrivileges", "yes"), ("PrivateTmp", "yes"), ("ProtectSystem", "strict"),
                           ("RestrictAddressFamilies", "AF_INET AF_INET6 AF_UNIX"), ("StateDirectory", "boswas"),
                           ("CacheDirectory", APP_ID), ("MemoryDenyWriteExecute", "yes"),
                           ("SystemCallFilter", "@system-service"), ("RestrictNamespaces", "yes")):
            self.assertEqual(section.get(key), value, key)
        self.assertRegex(section["MemoryMax"], r"^\d+M$")
        self.assertRegex(section["TimeoutStartSec"], r"^\d+$")
        self.assertLessEqual(int(section["TimeoutStartSec"]), 300)
        self.assertNotIn("ReadWritePaths", section, "it writes its two directories and nothing else")

    def test_the_package_installs_them(self):
        rules = (PACKAGE / "debian" / "rules").read_text(encoding="utf-8")
        for name in ("boswas-weather-update.service", "boswas-weather-update.timer"):
            self.assertIn(f"install -D -m 0644 systemd/{name} \\\n\t\t$(PKG)/usr/lib/systemd/user/{name}", rules)
        self.assertTrue((PACKAGE / "bin" / "boswas-weather").is_file())

    def test_the_manual_says_what_runs(self):
        manual = (PACKAGE / "man" / "boswas-weather.1").read_text(encoding="utf-8")
        for words in ("update\\-lock\\-screen", "boswas\\-weather\\-update.timer", "weather.json",
                      "show_weather"):
            self.assertIn(words, manual)


if __name__ == "__main__":
    unittest.main()
