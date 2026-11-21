"""Weather's model without Qt: reading both answers, the WMO table, units, formatting."""

import copy
import datetime as dt
import json
import unittest
from pathlib import Path

from weather_fixtures import answer

from boswas_apps.weather import model

ICON_DIRECTORIES = [Path(__file__).resolve().parents[4] / "desktop" / "icons" / "Boswas-Prism" / "scalable" / folder
                    for folder in ("actions", "status", "apps")]
DOCUMENTED_CODES = (0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86,
                    95, 96, 99)
UTC = dt.timezone.utc


class ForecastParsingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.berlin = answer("forecast-berlin.json")

    def test_the_recorded_answer(self):
        forecast = model.parse_forecast(self.berlin)
        self.assertEqual((forecast.timezone, forecast.utc_offset), ("Europe/Berlin", 3600))
        current = forecast.current
        self.assertEqual(current.time, dt.datetime(2026, 11, 21, 14, 15))
        self.assertEqual((current.temperature, current.apparent, current.humidity), (7.9, 4.6, 78.0))
        self.assertEqual((current.code, current.is_day, current.cloud_cover), (2, True, 64.0))
        self.assertEqual((current.wind_speed, current.wind_direction, current.wind_gusts), (14.8, 236.0, 31.3))
        self.assertEqual((current.pressure, current.visibility, current.uv_index), (1012.3, 24140.0, 0.65))
        self.assertEqual(len(forecast.hours), 240)
        self.assertEqual(forecast.hours[0].time, dt.datetime(2026, 11, 21, 0, 0))
        self.assertEqual(forecast.hours[-1].time, dt.datetime(2026, 11, 30, 23, 0))
        self.assertEqual(len(forecast.days), 10)
        today = forecast.days[0]
        self.assertEqual(today.date, dt.date(2026, 11, 21))
        self.assertEqual((today.temperature_min, today.temperature_max, today.code), (2.1, 8.2, 2))
        self.assertEqual((today.sunrise, today.sunset), (dt.datetime(2026, 11, 21, 7, 44),
                                                          dt.datetime(2026, 11, 21, 15, 59)))
        self.assertEqual(today.precipitation_probability_max, 8.0)

    def test_nulls_of_the_answer_stay_unknown(self):
        forecast = model.parse_forecast(self.berlin)
        self.assertIsNone(forecast.hours[200].temperature)
        self.assertIsNotNone(forecast.hours[200].code)
        self.assertIsNone(forecast.hours[201].code)
        self.assertIsNone(forecast.days[9].uv_index_max)
        self.assertIsNone(forecast.days[8].precipitation_probability_max)
        self.assertTrue(all(hour.precipitation_probability is None for hour in forecast.hours[8 * 24:]))

    def test_missing_optional_fields(self):
        sydney = model.parse_forecast(answer("forecast-sydney.json"))
        self.assertEqual(sydney.utc_offset, 11 * 3600)
        self.assertIsNone(sydney.current.visibility)
        self.assertIsNone(sydney.current.uv_index)
        self.assertEqual(sydney.current.is_day, False)
        self.assertEqual(sydney.current.temperature, 19.6)

    def test_time_of_the_place(self):
        forecast = model.parse_forecast(self.berlin)
        now = dt.datetime(2026, 11, 21, 13, 20, tzinfo=UTC).timestamp()
        local = forecast.local_now(now)
        self.assertEqual(local, dt.datetime(2026, 11, 21, 14, 20))
        hours = forecast.hours_from(local)
        self.assertEqual(len(hours), 24)
        self.assertEqual(hours[0].time, dt.datetime(2026, 11, 21, 14, 0))
        self.assertEqual(hours[-1].time, dt.datetime(2026, 11, 22, 13, 0))
        self.assertEqual([day.date.day for day in forecast.days_from(local.date() + dt.timedelta(days=2), 3)],
                         [23, 24, 25])
        self.assertEqual(forecast.hours_from(dt.datetime(2026, 12, 5, 9, 0)), [])
        self.assertEqual(forecast.day(dt.date(2026, 11, 22)).code, 61)
        self.assertIsNone(forecast.day(dt.date(2026, 12, 22)))

    def test_without_current_the_hour_is_used(self):
        del self.berlin["current"]
        forecast = model.parse_forecast(self.berlin)
        self.assertIsNone(forecast.current)
        conditions = forecast.conditions(dt.datetime(2026, 11, 21, 14, 20))
        self.assertEqual((conditions.time, conditions.temperature, conditions.code), (
            dt.datetime(2026, 11, 21, 14, 0), forecast.hours[14].temperature, forecast.hours[14].code))
        self.assertIsNone(conditions.humidity)
        self.assertIsNone(forecast.conditions(dt.datetime(2027, 1, 1)))

    def test_answers_that_hold_nothing(self):
        for bad in (None, [], "text", 12, {}, {"hourly": {}}, {"current": {"temperature_2m": "warm"}},
                    {"daily": {"time": ["yesterday"]}}):
            with self.assertRaises(model.DataError, msg=repr(bad)):
                model.parse_forecast(bad)
        with self.assertRaisesRegex(model.DataError, "Latitude must be in range"):
            model.parse_forecast({"error": True, "reason": "Latitude must be in range of -90 to 90°.\n‮"})

    def test_every_field_is_checked(self):
        bad = copy.deepcopy(self.berlin)
        current = bad["current"]
        current.update(temperature_2m=float("nan"), relative_humidity_2m=140, apparent_temperature=True,
                       is_day=3, weather_code=2, pressure_msl=10 ** 400, wind_speed_10m=-4,
                       wind_direction_10m="SW", visibility=None, cloud_cover=50)
        bad["utc_offset_seconds"] = 10 ** 9
        bad["timezone"] = "../../etc/passwd zone"
        forecast = model.parse_forecast(bad)
        parsed = forecast.current
        self.assertEqual((parsed.temperature, parsed.humidity, parsed.apparent, parsed.is_day, parsed.code),
                         (None, None, None, None, 2))
        self.assertEqual([model.weather_code(v) for v in (2.5, -1, 100, "3", True, 3.0)],
                         [None, None, None, None, None, 3])
        self.assertEqual((parsed.pressure, parsed.wind_speed, parsed.wind_direction, parsed.visibility),
                         (None, None, None, None))
        self.assertEqual(parsed.cloud_cover, 50.0)
        self.assertEqual((forecast.utc_offset, forecast.timezone), (0, ""))

    def test_a_current_without_temperature_and_code_is_not_now(self):
        self.berlin["current"] = {"time": "2026-11-21T14:15", "relative_humidity_2m": 70}
        self.assertIsNone(model.parse_forecast(self.berlin).current)

    def test_columns_of_other_lengths_and_bad_times(self):
        hourly = self.berlin["hourly"]
        hourly["temperature_2m"] = hourly["temperature_2m"][:10]          # shorter: the rest is unknown
        hourly["weather_code"] = hourly["weather_code"] + [3] * 50          # longer: cut
        hourly["time"][5] = "2026-11-21 05:00:00 UTC"                      # not a local ISO time: dropped
        hourly["time"][7] = hourly["time"][3]                              # out of order: dropped
        hourly["is_day"] = "yes"                                           # not an array: unknown
        forecast = model.parse_forecast(self.berlin)
        self.assertEqual(len(forecast.hours), 238)
        self.assertIsNotNone(forecast.hours[7].temperature)      # the hour of 09:00, the tenth value
        self.assertIsNone(forecast.hours[8].temperature)
        self.assertTrue(all(hour.is_day is None for hour in forecast.hours))
        times = [hour.time for hour in forecast.hours]
        self.assertEqual(times, sorted(set(times)))

    def test_units_other_than_metric_are_not_trusted(self):
        self.berlin["current_units"]["temperature_2m"] = "°F"
        self.berlin["hourly_units"]["wind_speed_10m"] = "mp/h"
        self.berlin["daily_units"]["precipitation_sum"] = "inch"
        forecast = model.parse_forecast(self.berlin)
        self.assertIsNone(forecast.current.temperature)
        self.assertEqual(forecast.current.apparent, 4.6)
        self.assertTrue(all(hour.wind_speed is None for hour in forecast.hours))
        self.assertTrue(all(day.precipitation_sum is None for day in forecast.days))

    def test_days(self):
        daily = self.berlin["daily"]
        daily["temperature_2m_max"][1], daily["temperature_2m_min"][1] = 1.0, 9.0        # swapped
        daily["sunrise"][2] = "2026-11-25T07:50"                                        # not that day
        daily["sunset"][3] = None
        daily["time"][4] = "2026-02-30"
        forecast = model.parse_forecast(self.berlin)
        self.assertEqual(len(forecast.days), 9)
        self.assertEqual((forecast.days[1].temperature_min, forecast.days[1].temperature_max), (1.0, 9.0))
        self.assertIsNone(forecast.days[2].sunrise)
        self.assertIsNotNone(forecast.days[2].sunset)
        self.assertIsNone(forecast.days[3].sunset)

    def test_the_answer_is_not_changed(self):
        before = json.dumps(self.berlin, sort_keys=True)
        model.parse_forecast(self.berlin)
        self.assertEqual(json.dumps(self.berlin, sort_keys=True), before)


class PlaceParsingTest(unittest.TestCase):
    def test_the_recorded_search(self):
        places = model.parse_places(answer("search-berlin.json"))
        self.assertEqual([place.name for place in places],
                         ["Berlin", "Berlin", "Berlin", "Berlin-Charlottenburg", "Berlín"])
        first = places[0]
        self.assertEqual((first.id, first.key, first.latitude, first.longitude), (2950159, "2950159", 52.52437,
                                                                                    13.41053))
        self.assertEqual((first.country, first.country_code, first.admin1, first.timezone),
                         ("Germany", "DE", "Land Berlin", "Europe/Berlin"))
        self.assertEqual(first.region(), "Land Berlin, Germany")
        self.assertEqual(places[1].label(), "Berlin, New Hampshire, United States")
        self.assertEqual(places[3].region(), "Germany")

    def test_nothing_found(self):
        self.assertEqual(model.parse_places(answer("search-nothing.json")), [])
        self.assertEqual(model.parse_places({"results": []}), [])

    def test_bad_answers(self):
        for bad in (None, [], "x", {"results": "Berlin"}, {"results": {"name": "Berlin"}}):
            with self.assertRaises(model.DataError, msg=repr(bad)):
                model.parse_places(bad)
        with self.assertRaisesRegex(model.DataError, "Parameter count"):
            model.parse_places({"error": True, "reason": "Parameter count must be between 1 and 100"})

    def test_every_place_is_checked(self):
        good = {"id": 1, "name": "Good", "latitude": 1.0, "longitude": 2.0}
        entries = [
            {"id": 2, "name": "North of the pole", "latitude": 91, "longitude": 0},
            {"id": 3, "latitude": 1, "longitude": 1},
            {"id": 4, "name": "   ", "latitude": 1, "longitude": 1},
            {"id": 5, "name": "Str", "latitude": "1", "longitude": 1},
            "not a place",
            {"id": True, "name": "Flag id", "latitude": 10.123456789, "longitude": -20.5, "country_code": "deu",
             "timezone": "Europe/Berlin; rm -rf", "admin1": 7},
            {"id": 6, "name": "Line\nbreak‮evil\u0000", "latitude": 1, "longitude": 1,
             "country": "C" * 500, "country_code": "fr"},
            {"id": 7, "name": "<img src='file:///etc/passwd'>Town", "latitude": 1, "longitude": 1,
             "admin1": "<b>Bold</b>"},
            good, dict(good),
        ]
        places = model.parse_places({"results": entries})
        self.assertEqual([place.name for place in places], ["Flag id", "Line break evil",
                                                            "img src='file:///etc/passwd'Town", "Good"])
        self.assertEqual(places[2].admin1, "bBold/b")
        self.assertFalse(any("<" in place.label() or ">" in place.label() for place in places))
        flag = places[0]
        self.assertIsNone(flag.id)
        self.assertEqual(flag.key, "geo_10.1235_-20.5000")
        self.assertEqual((flag.country_code, flag.timezone, flag.admin1), ("", "", ""))
        self.assertEqual(places[1].country_code, "FR")
        self.assertEqual(len(places[1].country), model.MAX_REGION)

    def test_at_most_a_number_of_results(self):
        entries = [{"id": n, "name": f"Place {n}", "latitude": 0, "longitude": n % 180} for n in range(1, 200)]
        self.assertEqual(len(model.parse_places({"results": entries})), model.MAX_RESULTS)

    def test_round_trip(self):
        place = model.parse_places(answer("search-berlin.json"))[0]
        self.assertEqual(model.Place.from_json(json.loads(json.dumps(place.to_json()))), place)


class ConditionTest(unittest.TestCase):
    def test_every_documented_code(self):
        icons = {path.stem for folder in ICON_DIRECTORIES for path in folder.glob("*.svg")}
        for code in DOCUMENTED_CODES:
            for is_day in (True, False):
                icon = model.condition_icon(code, is_day)
                text = model.condition_text(code, is_day)
                self.assertIn(icon, model.CONDITION_ICONS, code)
                self.assertIn(icon, icons, f"{icon} is not in the icon pack")
                self.assertTrue(text and text[0].isupper() and not text.endswith("."), code)
        self.assertEqual(set(model.WMO), set(DOCUMENTED_CODES))

    def test_the_mapping(self):
        expected = {0: "weather-clear-symbolic", 1: "weather-few-clouds-symbolic", 2: "weather-few-clouds-symbolic",
                    3: "weather-overcast-symbolic", 45: "weather-fog-symbolic", 48: "weather-fog-symbolic",
                    51: "weather-showers-scattered-symbolic", 57: "weather-showers-scattered-symbolic",
                    61: "weather-showers-symbolic", 65: "weather-showers-symbolic",
                    66: "weather-showers-symbolic", 67: "weather-showers-symbolic", 71: "weather-snow-symbolic",
                    77: "weather-snow-symbolic", 80: "weather-showers-scattered-symbolic",
                    82: "weather-showers-symbolic", 85: "weather-snow-symbolic", 86: "weather-snow-symbolic",
                    95: "weather-storm-symbolic", 96: "weather-storm-symbolic", 99: "weather-storm-symbolic"}
        for code, icon in expected.items():
            self.assertEqual(model.condition_icon(code), icon, code)

    def test_night(self):
        self.assertEqual(model.condition_icon(0, False), "weather-clear-night-symbolic")
        self.assertEqual(model.condition_icon(1, False), "weather-few-clouds-night-symbolic")
        self.assertEqual(model.condition_icon(2, False), "weather-few-clouds-night-symbolic")
        self.assertEqual(model.condition_icon(61, False), "weather-showers-symbolic")
        self.assertEqual((model.condition_text(0, True), model.condition_text(0, False)), ("Sunny", "Clear"))
        self.assertEqual(model.condition_icon(0, None), "weather-clear-symbolic")

    def test_unknown_codes(self):
        for code in (None, 4, 50, 98):
            self.assertEqual(model.condition_icon(code), "weather-clouds-symbolic")
            self.assertEqual(model.condition_text(code), "Unknown conditions")


class UnitTest(unittest.TestCase):
    def test_conversions(self):
        self.assertEqual(model.convert_temperature(0, "f"), 32)
        self.assertEqual(model.convert_temperature(100, "f"), 212)
        self.assertEqual(model.convert_temperature(-40, "f"), -40)
        self.assertEqual(model.convert_temperature(21.5, "c"), 21.5)
        self.assertAlmostEqual(model.convert_wind(100, "mph"), 62.137, places=3)
        self.assertAlmostEqual(model.convert_wind(36, "ms"), 10.0)
        self.assertAlmostEqual(model.convert_wind(18.52, "kn"), 10.0)
        self.assertEqual(model.convert_wind(12, "kmh"), 12)
        self.assertAlmostEqual(model.convert_precipitation(25.4, "in"), 1.0)
        self.assertAlmostEqual(model.convert_pressure(1013.25, "inhg"), 29.92, places=2)
        self.assertAlmostEqual(model.convert_distance(1609.344, "mi"), 1.0)
        self.assertEqual(model.convert_distance(2500, "km"), 2.5)
        self.assertEqual((model.distance_unit("mph"), model.distance_unit("kn")), ("mi", "km"))

    def test_rounding_as_people_round(self):
        self.assertEqual([model.whole(v) for v in (12.5, 12.49, -2.5, -0.4, 0.5, -12.5)], [13, 12, -3, 0, 1, -13])

    def test_formatting(self):
        self.assertEqual(model.format_temperature(7.9, "c", with_unit=True), "8°C")
        self.assertEqual(model.format_temperature(7.9, "f", with_unit=True), "46°F")
        self.assertEqual(model.format_temperature(-0.4, "c"), "0°")
        self.assertEqual(model.format_temperature(-4.1, "c"), "-4°")
        self.assertEqual(model.format_temperature(None, "c"), model.MISSING)
        self.assertEqual([model.format_wind(14.8, unit) for unit in ("kmh", "mph", "ms", "kn")],
                         ["15 km/h", "9 mph", "4.1 m/s", "8 kn"])
        self.assertEqual(model.format_precipitation(8.7, "mm"), "8.7 mm")
        self.assertEqual(model.format_precipitation(8.7, "in"), "0.34 in")
        self.assertEqual(model.format_pressure(1012.3, "hpa"), "1012 hPa")
        self.assertEqual(model.format_pressure(1012.3, "inhg"), "29.89 inHg")
        self.assertEqual(model.format_visibility(24140, "km"), "24 km")
        self.assertEqual(model.format_visibility(8500, "km"), "8.5 km")
        self.assertEqual(model.format_visibility(24140, "mi"), "15 mi")
        self.assertEqual(model.format_percent(77.6), "78%")
        self.assertEqual(model.format_uv(0.65), "1")
        for formatter in (model.format_wind, model.format_precipitation, model.format_pressure,
                          model.format_visibility):
            self.assertEqual(formatter(None, "x"), model.MISSING)
        self.assertEqual((model.format_percent(None), model.format_uv(None)), (model.MISSING, model.MISSING))

    def test_uv_categories(self):
        cases = {0: "Low", 2: "Low", 2.4: "Low", 2.5: "Moderate", 5: "Moderate", 6: "High", 7: "High",
                 8: "Very high", 10: "Very high", 11: "Extreme", 14: "Extreme"}
        for value, category in cases.items():
            self.assertEqual(model.uv_category(value), category, value)
        self.assertEqual(model.uv_category(None), "")

    def test_compass(self):
        cases = {0: "N", 11.24: "N", 11.25: "NNE", 45: "NE", 90: "E", 180: "S", 236: "SW", 270: "W", 348.75: "N",
                 359: "N", 360: "N", 315: "NW"}
        for degrees, point in cases.items():
            self.assertEqual(model.compass_point(degrees), point, degrees)
        self.assertEqual(model.compass_name(236), "southwest")
        self.assertEqual(model.compass_name(200), "south-southwest")
        self.assertEqual((model.compass_point(None), model.compass_name(None)), ("", ""))

    def test_wind_force_and_dew_point(self):
        self.assertEqual([model.beaufort(v) for v in (0, 5, 14.8, 40, 130)],
                         ["Calm", "Light air", "Gentle breeze", "Strong breeze", "Hurricane force"])
        self.assertEqual(model.beaufort(None), "")
        self.assertAlmostEqual(model.dew_point(20, 50), 9.26, places=1)
        self.assertAlmostEqual(model.dew_point(10, 100), 10.0, places=6)
        self.assertIsNone(model.dew_point(10, 0))
        self.assertIsNone(model.dew_point(None, 50))

    def test_words(self):
        self.assertEqual([model.visibility_words(v) for v in (24140, 5000, 2000, 300)],
                         ["Good visibility", "Moderate visibility", "Poor visibility", "Very poor visibility"])
        self.assertEqual([model.cloud_words(v) for v in (4, 30, 64, 80, 100)],
                         ["Clear sky", "A few clouds", "Partly cloudy", "Mostly cloudy", "Overcast"])


class TimeTest(unittest.TestCase):
    def test_clock(self):
        morning, noon, night = dt.time(7, 44), dt.time(12, 0), dt.time(0, 5)
        self.assertEqual(model.format_clock(morning, False), "07:44")
        self.assertEqual(model.format_clock(morning, True), "7:44 AM")
        self.assertEqual(model.format_clock(noon, True), "12:00 PM")
        self.assertEqual(model.format_clock(night, True), "12:05 AM")
        self.assertEqual(model.format_clock(dt.datetime(2026, 1, 1, 15, 59), True), "3:59 PM")
        self.assertEqual(model.format_clock(None, True), model.MISSING)
        self.assertEqual(model.format_hour(dt.time(15), False), "15:00")
        self.assertEqual(model.format_hour(dt.time(15), True), "3 PM")
        self.assertEqual(model.format_hour(dt.time(0), True), "12 AM")

    def test_days(self):
        today = dt.date(2026, 11, 21)
        self.assertEqual(model.day_label(today, today), "Today")
        self.assertEqual(model.day_label(dt.date(2026, 11, 22), today), "Sun 22")
        self.assertEqual(model.day_name(dt.date(2026, 11, 22), today), "Tomorrow")
        self.assertEqual(model.day_name(dt.date(2026, 11, 23), today), "Monday 23 Nov")
        self.assertEqual(model.format_date(today), "Sat 21 Nov")
        self.assertEqual(model.format_duration(8 * 3600 + 15 * 60 + 30), "8 h 15 min")
        self.assertEqual(model.format_duration(7200), "2 h")
        self.assertEqual(model.format_duration(59), "0 min")

    def test_updated(self):
        now = dt.datetime(2026, 11, 21, 13, 20, tzinfo=UTC).timestamp()
        self.assertEqual(model.updated_text(now - 600, now, False, UTC), "Updated at 13:10")
        self.assertEqual(model.updated_text(now + 30, now, True, UTC), "Updated at 1:20 PM")
        self.assertEqual(model.updated_text(now - 86400, now, False, UTC), "Updated yesterday at 13:20")
        self.assertEqual(model.updated_text(now - 3 * 86400, now, False, UTC), "Updated Wed 18 Nov at 13:20")


class ListTest(unittest.TestCase):
    def test_range_span(self):
        self.assertEqual(model.range_span(0, 10, -10, 10), (0.5, 1.0))
        self.assertEqual(model.range_span(-20, 30, -10, 10), (0.0, 1.0))
        self.assertEqual(model.range_span(5, 5, 5, 5), (0.0, 1.0))
        self.assertIsNone(model.range_span(None, 4, 0, 10))

    def test_descriptions(self):
        forecast = model.parse_forecast(answer("forecast-berlin.json"))
        hour = forecast.hours[14]
        self.assertEqual(model.describe_hour(hour, "c", False, now=True), "Now, Partly cloudy, 8°C, 7% chance of rain")
        self.assertEqual(model.describe_hour(hour, "f", True), "2 PM, Partly cloudy, 47°F, 7% chance of rain")
        day = forecast.days[2]
        self.assertEqual(model.describe_day(day, forecast.days[0].date, "c"),
                         "Monday 23 Nov, Rain, high 6°C, low 2°C, 91% chance of rain")
        self.assertNotIn("chance", model.describe_day(forecast.days[9], forecast.days[0].date, "c"))

    def test_clean_text(self):
        self.assertEqual(model.clean_text("  São\tPaulo \n", 50), "São Paulo")
        self.assertEqual(model.clean_text("a‮b⁦c", 50), "a b c")
        self.assertEqual(model.clean_text("x" * 300, 10), "x" * 10)
        self.assertEqual(model.clean_text(42, 10), "")


if __name__ == "__main__":
    unittest.main()
