"""Date calculation and unit conversion."""

from __future__ import annotations

import unittest
from datetime import date
from fractions import Fraction as F

from boswas_apps.calculator import dates, units


def span(first, second):
    result = dates.difference(date(*first), date(*second))
    return result.years, result.months, result.weeks, result.days, result.total_days


class DateDifferenceTest(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(span((2026, 1, 1), (2026, 1, 1)), (0, 0, 0, 0, 0))
        self.assertEqual(span((2026, 1, 1), (2026, 1, 2)), (0, 0, 0, 1, 1))
        self.assertEqual(span((2026, 1, 1), (2026, 1, 8)), (0, 0, 1, 0, 7))
        self.assertEqual(span((2026, 1, 1), (2026, 2, 1)), (0, 1, 0, 0, 31))
        self.assertEqual(span((2026, 1, 1), (2027, 1, 1)), (1, 0, 0, 0, 365))
        self.assertEqual(span((2025, 1, 1), (2026, 11, 15)), (1, 10, 2, 0, 683))
        self.assertEqual(span((2000, 1, 1), (2026, 11, 15)), (26, 10, 2, 0, 9815))

    def test_the_order_does_not_matter(self):
        self.assertEqual(span((2026, 11, 15), (2025, 1, 1)), span((2025, 1, 1), (2026, 11, 15)))

    def test_leap_years(self):
        self.assertEqual(span((2024, 2, 28), (2024, 3, 1)), (0, 0, 0, 2, 2))
        self.assertEqual(span((2023, 2, 28), (2023, 3, 1)), (0, 0, 0, 1, 1))
        self.assertEqual(span((2024, 1, 1), (2025, 1, 1)), (1, 0, 0, 0, 366))
        self.assertEqual(span((2020, 2, 29), (2024, 2, 29)), (4, 0, 0, 0, 1461))
        self.assertEqual(span((2024, 2, 29), (2025, 2, 28)), (1, 0, 0, 0, 365))
        self.assertEqual(span((2024, 2, 29), (2025, 3, 1)), (1, 0, 0, 1, 366))
        self.assertEqual(span((1900, 2, 28), (1900, 3, 1)), (0, 0, 0, 1, 1))      # 1900 was no leap year
        self.assertEqual(span((2000, 2, 28), (2000, 3, 1)), (0, 0, 0, 2, 2))      # 2000 was one
        self.assertEqual(span((2023, 12, 31), (2024, 12, 31)), (1, 0, 0, 0, 366))

    def test_month_ends(self):
        self.assertEqual(span((2023, 1, 31), (2023, 2, 28)), (0, 1, 0, 0, 28))
        self.assertEqual(span((2024, 1, 31), (2024, 2, 29)), (0, 1, 0, 0, 29))
        self.assertEqual(span((2024, 1, 31), (2024, 2, 28)), (0, 0, 4, 0, 28))
        self.assertEqual(span((2023, 1, 31), (2023, 3, 30)), (0, 1, 4, 2, 58))
        self.assertEqual(span((2023, 1, 31), (2023, 3, 31)), (0, 2, 0, 0, 59))
        self.assertEqual(span((2023, 3, 31), (2023, 4, 30)), (0, 1, 0, 0, 30))
        self.assertEqual(span((2023, 8, 31), (2023, 9, 30)), (0, 1, 0, 0, 30))
        self.assertEqual(span((2023, 1, 30), (2023, 3, 1)), (0, 1, 0, 1, 30))

    def test_whole_range(self):
        result = dates.difference(dates.MIN_DATE, dates.MAX_DATE)
        self.assertEqual((result.years, result.months, result.total_days), (9998, 11, 3652058))

    def test_text(self):
        def text(first, second):
            result = dates.difference(date(*first), date(*second))
            return dates.describe(result), dates.describe_total(result)
        self.assertEqual(text((2026, 1, 1), (2026, 1, 1)), ("Same dates", ""))
        self.assertEqual(text((2026, 1, 1), (2026, 1, 2)), ("1 day", ""))
        self.assertEqual(text((2026, 1, 1), (2026, 1, 4)), ("3 days", ""))
        self.assertEqual(text((2026, 1, 1), (2026, 1, 8)), ("1 week", "7 days"))
        self.assertEqual(text((2025, 1, 1), (2026, 11, 15)), ("1 year, 10 months, 2 weeks", "683 days"))
        self.assertEqual(text((2024, 1, 1), (2026, 3, 18)), ("2 years, 2 months, 2 weeks, 3 days", "807 days"))
        self.assertEqual(text((2026, 1, 1), (2027, 1, 1)), ("1 year", "365 days"))


class DateShiftTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(dates.shift(date(2026, 11, 15), days=30), date(2026, 12, 15))
        self.assertEqual(dates.shift(date(2026, 11, 15), months=2), date(2027, 1, 15))
        self.assertEqual(dates.shift(date(2026, 11, 15), years=1, months=1, days=1), date(2027, 12, 16))
        self.assertEqual(dates.shift(date(2024, 1, 31), months=1), date(2024, 2, 29))
        self.assertEqual(dates.shift(date(2023, 1, 31), months=1), date(2023, 2, 28))
        self.assertEqual(dates.shift(date(2024, 2, 29), years=1), date(2025, 2, 28))
        self.assertEqual(dates.shift(date(2024, 2, 29), years=4), date(2028, 2, 29))
        self.assertEqual(dates.shift(date(2023, 12, 31), days=1), date(2024, 1, 1))
        self.assertEqual(dates.shift(date(2024, 2, 28), days=2), date(2024, 3, 1))
        self.assertEqual(dates.shift(date(2023, 10, 31), months=13), date(2024, 11, 30))
        self.assertEqual(dates.shift(date(2023, 1, 31), months=1, days=1), date(2023, 3, 1))
        self.assertEqual(dates.shift(date(2026, 5, 5)), date(2026, 5, 5))

    def test_subtract(self):
        self.assertEqual(dates.shift(date(2026, 11, 15), days=30, subtract=True), date(2026, 10, 16))
        self.assertEqual(dates.shift(date(2023, 3, 31), months=1, subtract=True), date(2023, 2, 28))
        self.assertEqual(dates.shift(date(2024, 3, 31), months=1, subtract=True), date(2024, 2, 29))
        self.assertEqual(dates.shift(date(2024, 2, 29), years=1, subtract=True), date(2023, 2, 28))
        self.assertEqual(dates.shift(date(2024, 1, 1), days=1, subtract=True), date(2023, 12, 31))
        self.assertEqual(dates.shift(date(2024, 3, 1), days=1, subtract=True), date(2024, 2, 29))
        self.assertEqual(dates.shift(date(2026, 1, 15), years=2, months=3, days=20, subtract=True), date(2023, 9, 25))

    def test_out_of_range(self):
        with self.assertRaises(dates.DateRangeError):
            dates.shift(date(9999, 6, 1), years=1)
        with self.assertRaises(dates.DateRangeError):
            dates.shift(date(9999, 12, 31), days=1)
        with self.assertRaises(dates.DateRangeError):
            dates.shift(date(1, 1, 1), days=1, subtract=True)
        with self.assertRaises(dates.DateRangeError):
            dates.shift(date(1, 6, 1), months=6, subtract=True)
        self.assertEqual(dates.shift(date(9000, 1, 1), years=999), date(9999, 1, 1))

    def test_spans_are_validated(self):
        for bad in (-1, 1000, 1.5, "3", None, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    dates.shift(date(2026, 1, 1), days=bad)

    def test_add_then_difference(self):
        start = date(2020, 2, 29)
        for years, months, days in ((0, 0, 1), (1, 0, 0), (0, 11, 20), (3, 5, 17), (0, 1, 6)):
            with self.subTest(span=(years, months, days)):
                end = dates.shift(start, years=years, months=months, days=days)
                result = dates.difference(start, end)
                self.assertEqual((result.years, result.months, result.weeks * 7 + result.days), (years, months, days))


def convert(category, value, source, target, typed=None):
    kind = units.category(category)
    result = units.convert(F(value), kind.unit(source), kind.unit(target))
    return units.format_result(result, units.significant_digits(typed if typed is not None else str(value)))


class UnitTest(unittest.TestCase):
    def test_every_category_is_there(self):
        self.assertEqual([category.name for category in units.CATEGORIES],
                         ["Length", "Weight and mass", "Temperature", "Area", "Volume", "Speed", "Time", "Data",
                          "Energy", "Power", "Pressure", "Angle"])
        for category in units.CATEGORIES:
            with self.subTest(category=category.key):
                keys = category.keys()
                self.assertEqual(len(keys), len(set(keys)))
                self.assertIn(category.default_from, keys)
                self.assertIn(category.default_to, keys)
                self.assertGreaterEqual(len(keys), 3)
                for unit in category.units:
                    self.assertGreater(unit.factor, 0)
                    self.assertTrue(unit.name and unit.symbol)
        with self.assertRaises(KeyError):
            units.category("currency")
        with self.assertRaises(KeyError):
            units.LENGTH.unit("parsec")

    def test_every_conversion_goes_there_and_back_exactly(self):
        value = F("123.456")
        for category in units.CATEGORIES:
            for source in category.units:
                for target in category.units:
                    there = units.convert(value, source, target)
                    self.assertEqual(units.convert(there, target, source), value,
                                     f"{category.key}: {source.key} -> {target.key}")

    def test_known_values(self):
        rows = [
            ("length", 1, "inch", "centimeter", "2.54"), ("length", 1, "foot", "meter", "0.3048"),
            ("length", 1, "yard", "meter", "0.9144"), ("length", 1, "mile", "kilometer", "1.609344"),
            ("length", 1, "kilometer", "mile", "0.6213712"), ("length", 1, "nautical_mile", "meter", "1,852"),
            ("length", 1, "mile", "foot", "5,280"), ("length", 12, "inch", "foot", "1"),
            ("length", 1, "meter", "nanometer", "1,000,000,000"), ("length", 1, "micron", "millimeter", "0.001"),
            ("length", 1, "angstrom", "nanometer", "0.1"), ("length", 100, "centimeter", "inch", "39.37008"),
            ("weight", 1, "pound", "kilogram", "0.4535924"), ("weight", 1, "kilogram", "pound", "2.204623"),
            ("weight", 1, "stone", "pound", "14"), ("weight", 1, "ounce", "gram", "28.34952"),
            ("weight", 1, "pound", "ounce", "16"), ("weight", 1, "short_ton", "pound", "2,000"),
            ("weight", 1, "long_ton", "kilogram", "1,016.047"), ("weight", 5, "carat", "gram", "1"),
            ("weight", 1, "tonne", "kilogram", "1,000"), ("weight", 1, "dekagram", "gram", "10"),
            ("weight", 1, "hectogram", "decigram", "1,000"), ("weight", 1, "centigram", "milligram", "10"),
            ("temperature", 100, "celsius", "fahrenheit", "212"), ("temperature", 0, "celsius", "fahrenheit", "32"),
            ("temperature", -40, "fahrenheit", "celsius", "-40"), ("temperature", 0, "kelvin", "celsius", "-273.15"),
            ("temperature", 32, "fahrenheit", "kelvin", "273.15"),
            ("temperature", "98.6", "fahrenheit", "celsius", "37"),
            ("temperature", 0, "celsius", "kelvin", "273.15"), ("temperature", 0, "fahrenheit", "celsius", "-17.77778"),
            ("temperature", 300, "kelvin", "fahrenheit", "80.33"),
            ("area", 1, "acre", "square_meter", "4,046.856"), ("area", 1, "square_mile", "acre", "640"),
            ("area", 1, "hectare", "acre", "2.471054"), ("area", 1, "square_foot", "square_inch", "144"),
            ("area", 1, "square_yard", "square_foot", "9"), ("area", 1, "square_kilometer", "hectare", "100"),
            ("area", 1, "square_meter", "square_centimeter", "10,000"),
            ("area", 1, "square_centimeter", "square_millimeter", "100"),
            ("volume", 1, "gallon_us", "liter", "3.785412"), ("volume", 1, "gallon_uk", "liter", "4.54609"),
            ("volume", 1, "cup_us", "milliliter", "236.5882"), ("volume", 1, "gallon_us", "cubic_inch", "231"),
            ("volume", 1, "gallon_us", "quart_us", "4"), ("volume", 1, "quart_us", "pint_us", "2"),
            ("volume", 1, "pint_us", "fluid_ounce_us", "16"), ("volume", 1, "fluid_ounce_us", "tablespoon_us", "2"),
            ("volume", 1, "tablespoon_us", "teaspoon_us", "3"), ("volume", 1, "pint_uk", "fluid_ounce_uk", "20"),
            ("volume", 1, "gallon_uk", "pint_uk", "8"), ("volume", 1, "quart_uk", "pint_uk", "2"),
            ("volume", 1, "fluid_ounce_uk", "milliliter", "28.41306"),
            ("volume", 1, "tablespoon_uk", "milliliter", "17.75816"),
            ("volume", 1, "teaspoon_uk", "milliliter", "5.919388"), ("volume", 1, "cubic_foot", "liter", "28.31685"),
            ("volume", 1, "cubic_meter", "liter", "1,000"), ("volume", 1, "cubic_yard", "cubic_foot", "27"),
            ("volume", 1, "milliliter", "cubic_centimeter", "1"),
            ("speed", 100, "kilometer_per_hour", "mile_per_hour", "62.13712"),
            ("speed", 1, "knot", "kilometer_per_hour", "1.852"), ("speed", 1, "mach", "kilometer_per_hour", "1,225.08"),
            ("speed", 60, "mile_per_hour", "foot_per_second", "88"),
            ("speed", 1, "meter_per_second", "kilometer_per_hour", "3.6"),
            ("speed", 1, "meter_per_second", "centimeter_per_second", "100"),
            ("time", 1, "year", "day", "365.25"), ("time", 1, "week", "hour", "168"), ("time", 90, "minute", "hour", "1.5"),
            ("time", 1, "day", "second", "86,400"), ("time", 1, "second", "millisecond", "1,000"),
            ("time", 1, "millisecond", "microsecond", "1,000"), ("time", 1, "hour", "minute", "60"),
            ("data", 1, "gigabyte", "megabyte", "1,000"), ("data", 1, "gibibyte", "megabyte", "1,073.742"),
            ("data", 1, "byte", "bit", "8"), ("data", 1, "tebibyte", "gibibyte", "1,024"),
            ("data", 1, "kibibyte", "byte", "1,024"), ("data", 1, "kilobyte", "byte", "1,000"),
            ("data", 1, "megabit", "kilobyte", "125"), ("data", 1, "nibble", "bit", "4"),
            ("data", 1, "yobibyte", "zebibyte", "1,024"), ("data", 1, "exabyte", "petabyte", "1,000"),
            ("data", 8, "mebibit", "mebibyte", "1"),
            ("energy", 1, "food_calorie", "joule", "4,184"), ("energy", 1, "calorie", "joule", "4.184"),
            ("energy", 1, "btu", "joule", "1,055.056"), ("energy", 1, "kilowatt_hour", "kilojoule", "3,600"),
            ("energy", 1, "foot_pound", "joule", "1.355818"), ("energy", 1, "electron_volt", "joule", "1.602177e-19"),
            ("energy", 1, "kilojoule", "food_calorie", "0.2390057"),
            ("power", 1, "horsepower", "watt", "745.6999"), ("power", 1, "kilowatt", "horsepower", "1.341022"),
            ("power", 1, "btu_per_minute", "watt", "17.58426"),
            ("power", 1, "horsepower", "foot_pound_per_minute", "33,000"), ("power", 1, "kilowatt", "watt", "1,000"),
            ("pressure", 1, "atmosphere", "pascal", "101,325"), ("pressure", 1, "atmosphere", "psi", "14.69595"),
            ("pressure", 1, "psi", "pascal", "6,894.757"), ("pressure", 1, "bar", "atmosphere", "0.9869233"),
            ("pressure", 1, "atmosphere", "mmhg", "759.9999"), ("pressure", 1, "bar", "kilopascal", "100"),
            ("pressure", 1, "atmosphere", "bar", "1.01325"),
            ("angle", 180, "degree", "radian", "3.141593"), ("angle", 90, "degree", "gradian", "100"),
            ("angle", 1, "radian", "degree", "57.29578"), ("angle", 400, "gradian", "degree", "360"),
            ("angle", 200, "gradian", "radian", "3.141593"),
        ]
        for category, value, source, target, expected in rows:
            with self.subTest(conversion=f"{value} {source} -> {target}"):
                self.assertEqual(convert(category, value, source, target), expected)

    def test_exact_definitions(self):
        length = units.LENGTH
        self.assertEqual(units.convert(F(1), length.unit("inch"), length.unit("meter")), F("0.0254"))
        self.assertEqual(units.convert(F(1), length.unit("mile"), length.unit("meter")), F("1609.344"))
        self.assertEqual(units.convert(F(1), units.WEIGHT.unit("pound"), units.WEIGHT.unit("kilogram")),
                         F("0.45359237"))
        self.assertEqual(units.convert(F(1), units.VOLUME.unit("gallon_us"), units.VOLUME.unit("liter")),
                         F("3.785411784"))
        self.assertEqual(units.convert(F(1), units.AREA.unit("acre"), units.AREA.unit("square_meter")),
                         F("4046.8564224"))
        self.assertEqual(units.convert(F(1), units.PRESSURE.unit("psi"), units.PRESSURE.unit("pascal")),
                         F("0.45359237") * F("9.80665") / F("0.0254") ** 2)
        self.assertEqual(units.convert(F(1), units.POWER.unit("horsepower"), units.POWER.unit("watt")),
                         F("745.69987158227022"))
        self.assertEqual(units.convert(F("98.6"), units.TEMPERATURE.unit("fahrenheit"),
                                       units.TEMPERATURE.unit("celsius")), 37)
        self.assertEqual(units.convert(F(5), length.unit("meter"), length.unit("meter")), 5)

    def test_result_format(self):
        self.assertEqual(units.format_result(F(0)), "0")
        self.assertEqual(units.format_result(F("0.62137119223733")), "0.6213712")
        self.assertEqual(units.format_result(F("0.62137119223733"), 10), "0.6213711922")
        self.assertEqual(units.format_result(F("0.62137119223733"), 40), "0.62137119223733")
        self.assertEqual(units.format_result(F("123456789.012")), "123,456,789")
        self.assertEqual(units.format_result(F("123456789.012"), 12), "123,456,789.012")
        self.assertEqual(units.format_result(F(10) ** 20), "1.e+20")
        self.assertEqual(units.format_result(F(1, 10 ** 12)), "1.e-12")
        self.assertEqual(units.format_result(F("0.00001234")), "0.00001234")
        self.assertEqual(units.format_result(F("-17.7777777")), "-17.77778")
        self.assertEqual(units.format_result(F("1234.5"), decimal_point=",", group_separator="."), "1.234,5")
        self.assertEqual(units.significant_digits("0.050"), 2)
        self.assertEqual(units.significant_digits("1,234.50"), 6)
        self.assertEqual(units.significant_digits("0"), 1)

    def test_only_temperature_and_angle_take_a_sign(self):
        self.assertEqual([category.key for category in units.CATEGORIES if category.negative],
                         ["temperature", "angle"])


class ConverterInputTest(unittest.TestCase):
    def type(self, keys: str, negative: bool = False) -> units.ConverterInput:
        entry = units.ConverterInput()
        for key in keys.split():
            entry.press(key, allow_negative=negative)
        return entry

    def test_typing(self):
        self.assertEqual(self.type("1 2 . 5").text, "12.5")
        self.assertEqual(self.type("0 0 7").text, "7")
        self.assertEqual(self.type(". 5").text, "0.5")
        self.assertEqual(self.type("1 . . 5").text, "1.5")
        self.assertEqual(self.type("1 2 3 4 5 6 7").display(), "1,234,567")
        self.assertEqual(self.type("1 2 . 5 0").display(",", "."), "12,50")
        self.assertEqual(self.type("1 2 . 5").value(), F(25, 2))
        self.assertEqual(self.type("7 .").value(), 7)

    def test_backspace_and_clear(self):
        self.assertEqual(self.type("1 2 3 back").text, "12")
        self.assertEqual(self.type("1 back").text, "0")
        self.assertEqual(self.type("1 back back back").text, "0")
        self.assertEqual(self.type("1 . 5 back back").text, "1")
        self.assertEqual(self.type("1 2 ce").text, "0")
        self.assertEqual(self.type("5 sign back", True).text, "0")

    def test_sign(self):
        self.assertEqual(self.type("5 sign").text, "5")
        self.assertEqual(self.type("5 sign", True).text, "-5")
        self.assertEqual(self.type("5 sign sign", True).text, "5")
        self.assertEqual(self.type("sign", True).text, "0")
        self.assertEqual(self.type("4 0 sign", True).value(), -40)
        self.assertEqual(self.type("4 0 sign", True).display(), "-40")

    def test_limits_and_unknown_keys(self):
        entry = self.type(" ".join("9" * 20))
        self.assertEqual(entry.text, "9" * units.MAX_INPUT_DIGITS)
        self.assertFalse(entry.press("9"))
        self.assertFalse(entry.press("x"))
        self.assertFalse(entry.press(""))
        self.assertFalse(entry.press("12"))
        self.assertFalse(units.ConverterInput().press("back"))
        self.assertFalse(units.ConverterInput().press("ce"))
        self.assertTrue(units.ConverterInput().press("."))

    def test_set_value(self):
        entry = units.ConverterInput()
        entry.set_value(F("1234.5"))
        self.assertEqual(entry.text, "1234.5")
        entry.set_value(F(0))
        self.assertEqual(entry.text, "0")
        entry.set_value(F(-3))
        self.assertEqual(entry.text, "-3")
        entry.set_value(F(10) ** 40)
        self.assertEqual(entry.text, "0")


if __name__ == "__main__":
    unittest.main()
