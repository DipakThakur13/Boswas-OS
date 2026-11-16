"""Time zones and local times: the rules every schedule relies on."""

import datetime as dt
import os
import unittest
from unittest import mock

from boswas_apps.reminders import jsonfile, paths, textfmt, timeutil

BERLIN = timeutil.zone("Europe/Berlin")
NEW_YORK = timeutil.zone("America/New_York")
LORD_HOWE = timeutil.zone("Australia/Lord_Howe")


def utc(*args) -> float:
    return dt.datetime(*args, tzinfo=timeutil.UTC).timestamp()


class ToEpochTest(unittest.TestCase):
    def test_plain_times(self):
        self.assertEqual(timeutil.to_epoch(dt.datetime(2026, 1, 15, 12), BERLIN), utc(2026, 1, 15, 11))
        self.assertEqual(timeutil.to_epoch(dt.datetime(2026, 7, 15, 12), BERLIN), utc(2026, 7, 15, 10))
        self.assertEqual(timeutil.to_epoch(dt.datetime(2026, 7, 15, 12), timeutil.UTC), utc(2026, 7, 15, 12))

    def test_a_time_in_the_gap_is_read_with_the_offset_before_it(self):
        # 29 March 2026: 02:00 CET jumps to 03:00 CEST. 02:30 does not exist and becomes 03:30.
        instant = timeutil.to_epoch(dt.datetime(2026, 3, 29, 2, 30), BERLIN)
        self.assertEqual(instant, utc(2026, 3, 29, 1, 30))
        self.assertEqual(timeutil.from_epoch(instant, BERLIN), dt.datetime(2026, 3, 29, 3, 30))
        self.assertFalse(timeutil.exists(dt.datetime(2026, 3, 29, 2, 30), BERLIN))
        self.assertTrue(timeutil.exists(dt.datetime(2026, 3, 29, 3, 30), BERLIN))

    def test_a_time_that_happens_twice_means_the_first(self):
        # 25 October 2026: 03:00 CEST falls back to 02:00 CET. 02:30 happens at 00:30 and 01:30 UTC.
        self.assertEqual(timeutil.to_epoch(dt.datetime(2026, 10, 25, 2, 30), BERLIN), utc(2026, 10, 25, 0, 30))
        self.assertTrue(timeutil.is_ambiguous(dt.datetime(2026, 10, 25, 2, 30), BERLIN))
        self.assertFalse(timeutil.is_ambiguous(dt.datetime(2026, 10, 25, 3, 30), BERLIN))
        self.assertFalse(timeutil.is_ambiguous(dt.datetime(2026, 3, 29, 2, 30), BERLIN))

    def test_half_hour_shift(self):
        # Lord Howe Island moves its clocks by 30 minutes.
        before = timeutil.utc_offset(utc(2026, 1, 15), LORD_HOWE)
        after = timeutil.utc_offset(utc(2026, 7, 15), LORD_HOWE)
        self.assertEqual(before - after, 1800)

    def test_round_trip_every_hour_of_a_year(self):
        instant = utc(2026, 1, 1)
        for _ in range(0, 366 * 24):
            local = timeutil.from_epoch(instant, NEW_YORK)
            back = timeutil.to_epoch(local, NEW_YORK)
            # The repeated hour maps to its first occurrence: one hour earlier, never later.
            self.assertIn(instant - back, (0, 3600))
            instant += 3600

    def test_instants_out_of_range_do_not_raise(self):
        self.assertEqual(timeutil.to_aware(1e18, BERLIN).year, dt.MAXYEAR)
        self.assertEqual(timeutil.to_aware(-1e18, BERLIN).year, dt.MINYEAR)


class ZoneTest(unittest.TestCase):
    def test_names_are_checked_before_they_are_looked_up(self):
        for bad in ("", "../etc/passwd", "Europe/../../etc/passwd", "/etc/localtime", "Europe/Berlin\0", "a" * 100,
                    "Europe//Berlin", 7, None):
            self.assertIsNone(timeutil.zone(bad) if isinstance(bad, str) else None)
            self.assertFalse(timeutil.valid_zone_name(bad))
        self.assertIsNone(timeutil.zone("Europe/Nowhere"))
        self.assertIsNotNone(timeutil.zone("America/Argentina/Buenos_Aires"))

    def test_zone_names_are_all_usable(self):
        names = timeutil.zone_names()
        self.assertGreater(len(names), 300)
        self.assertIn("Europe/Berlin", names)
        self.assertEqual(list(names), sorted(names))

    def test_system_zone_from_the_tz_variable(self):
        with mock.patch.dict(os.environ, {"TZ": ":Asia/Tokyo"}):
            self.assertEqual(timeutil.system_zone_name(), "Asia/Tokyo")
            self.assertEqual(timeutil.zone_key(timeutil.system_zone()), "Asia/Tokyo")
        with mock.patch.dict(os.environ, {"TZ": "/usr/share/zoneinfo/Europe/Paris"}):
            self.assertEqual(timeutil.system_zone_name(), "Europe/Paris")
        with mock.patch.dict(os.environ, {"TZ": "XYZ-3"}):          # a POSIX rule, not a database name
            self.assertIsNone(timeutil.system_zone_name())
            self.assertIsNotNone(timeutil.system_zone())

    def test_system_zone_from_etc_localtime(self):
        environment = {key: value for key, value in os.environ.items() if key != "TZ"}
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch("os.path.realpath", return_value="/usr/share/zoneinfo/posix/Europe/Rome"):
            self.assertEqual(timeutil.system_zone_name(), "Europe/Rome")

    def test_zone_key(self):
        self.assertEqual(timeutil.zone_key(timeutil.UTC), "UTC")
        self.assertEqual(timeutil.zone_key(dt.timezone(dt.timedelta(hours=5, minutes=30))), "+05:30")
        self.assertEqual(timeutil.zone_key(dt.timezone(dt.timedelta(hours=-3))), "-03:00")


class TextTest(unittest.TestCase):
    def test_clock_time(self):
        self.assertEqual(textfmt.clock_time(7, 5), "07:05")
        self.assertEqual(textfmt.clock_time(0, 0, True), "12:00 AM")
        self.assertEqual(textfmt.clock_time(12, 30, True), "12:30 PM")
        self.assertEqual(textfmt.clock_time(23, 59, True), "11:59 PM")

    def test_until_words(self):
        self.assertEqual(textfmt.until_words(7 * 3600 + 12 * 60), "in 7 hours 12 minutes")
        self.assertEqual(textfmt.until_words(7 * 3600 + 11 * 60 + 1), "in 7 hours 12 minutes")
        self.assertEqual(textfmt.until_words(60), "in 1 minute")
        self.assertEqual(textfmt.until_words(59), "in less than a minute")
        self.assertEqual(textfmt.until_words(86400 + 2 * 3600 + 5), "in 1 day 2 hours")
        self.assertEqual(textfmt.until_words(3600), "in 1 hour")
        self.assertEqual(textfmt.until_words(0), "now")

    def test_countdown_and_stopwatch(self):
        self.assertEqual(textfmt.countdown(300), "05:00")
        self.assertEqual(textfmt.countdown(0.2), "00:01")
        self.assertEqual(textfmt.countdown(0), "00:00")
        self.assertEqual(textfmt.countdown(3600 + 65), "1:01:05")
        self.assertEqual(textfmt.countdown(59.001), "01:00")
        self.assertEqual(textfmt.stopwatch(83.456), "00:01:23.45")
        self.assertEqual(textfmt.stopwatch(0), "00:00:00.00")
        self.assertEqual(textfmt.stopwatch(3600 * 100 + 0.999), "100:00:00.99")

    def test_weekdays_and_offsets(self):
        self.assertEqual(textfmt.weekdays_text([]), "Once")
        self.assertEqual(textfmt.weekdays_text([0, 1, 2, 3, 4]), "Weekdays")
        self.assertEqual(textfmt.weekdays_text([6, 5]), "Weekends")
        self.assertEqual(textfmt.weekdays_text(range(7)), "Every day")
        self.assertEqual(textfmt.weekdays_text([0, 2, 4]), "Mon, Wed, Fri")
        self.assertEqual(textfmt.offset_words(0), "Same time")
        self.assertEqual(textfmt.offset_words(3 * 3600), "3 hours ahead")
        self.assertEqual(textfmt.offset_words(-(5 * 3600 + 1800)), "5 hours 30 minutes behind")

    def test_dates_and_lead_times(self):
        today = dt.date(2026, 11, 15)
        self.assertEqual(textfmt.short_date(today, today), "Today")
        self.assertEqual(textfmt.short_date(dt.date(2026, 11, 16), today), "Tomorrow")
        self.assertEqual(textfmt.short_date(dt.date(2026, 11, 14), today), "Yesterday")
        self.assertEqual(textfmt.short_date(dt.date(2026, 11, 17), today), "Tue 17 Nov")
        self.assertEqual(textfmt.short_date(dt.date(2027, 1, 5), today), "Tue 5 Jan 2027")
        self.assertEqual(textfmt.long_date(dt.date(2026, 11, 17)), "Tuesday, 17 November 2026")
        self.assertEqual(textfmt.before_words(0), "At time of event")
        self.assertEqual(textfmt.before_words(15), "15 minutes before")
        self.assertEqual(textfmt.before_words(60), "1 hour before")
        self.assertEqual(textfmt.before_words(2880), "2 days before")
        self.assertEqual(textfmt.before_words(10080), "1 week before")
        self.assertEqual(textfmt.before_words(-540), "9 hours after")
        self.assertEqual(textfmt.before_words(90), "90 minutes before")


class PathsTest(unittest.TestCase):
    def test_directories_follow_the_xdg_variables(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/x/data", "XDG_STATE_HOME": "/x/state",
                                          "XDG_CONFIG_HOME": "relative/is/ignored"}):
            self.assertEqual(str(paths.clock_store()), "/x/data/com.boswas.Clock/clock.json")
            self.assertEqual(str(paths.calendar_store()), "/x/data/com.boswas.Calendar/calendar.json")
            self.assertEqual(str(paths.service_state()), "/x/state/com.boswas.Reminders/state.json")
            self.assertTrue(str(paths.clock_settings()).endswith("/.config/com.boswas.Clock/settings.json"))

    def test_the_same_directories_as_the_toolkit(self):
        try:
            import boswas_fluent as fluent
        except ImportError:
            self.skipTest("the toolkit is not on the path")
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "XDG_DATA_HOME": tmp + "/d", "XDG_CONFIG_HOME": tmp + "/c", "XDG_STATE_HOME": tmp + "/s"}):
            self.assertEqual(paths.clock_store().parent, fluent.data_dir(paths.CLOCK_ID))
            self.assertEqual(paths.calendar_store().parent, fluent.data_dir(paths.CALENDAR_ID))
            self.assertEqual(paths.clock_settings().parent, fluent.config_dir(paths.CLOCK_ID))
            self.assertEqual(paths.service_state().parent, fluent.state_dir(paths.REMINDERS_ID))

    def test_runtime_dir_must_be_private(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o755)
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
                self.assertIsNone(paths.runtime_dir())
                self.assertNotIn(tmp, str(paths.service_lock()))
            os.chmod(tmp, 0o700)
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
                self.assertEqual(str(paths.runtime_dir()), tmp)
                self.assertEqual(str(paths.service_lock()), tmp + "/boswas-reminders.lock")


if __name__ == "__main__":
    unittest.main()
