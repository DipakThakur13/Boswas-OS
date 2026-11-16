"""Unit conversion with exact factors (standard library only, no Qt).

Every unit is defined against the base unit of its category by a rational
factor, taken from the definition of the unit (the inch is 0.0254 m, the
pound 0.45359237 kg, the US gallon 231 cubic inches, the thermochemical
calorie 4.184 J, the standard atmosphere 101325 Pa, ...), so a conversion
is exact rational arithmetic and ``1 in`` is ``2.54 cm`` without a rounding
error. Temperatures have an offset as well. Three factors are not exact
because no exact value exists: the radian (180/pi degrees, with the
42-digit pi of ``numeric``) and Mach (340.3 m/s, the speed of sound at sea
level that Windows Calculator uses); the year is 365.25 days.

There is no currency: rates need the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from . import numeric

F = Fraction


@dataclass(frozen=True)
class Unit:
    key: str
    name: str
    symbol: str
    factor: Fraction                  # base units in one of this unit
    offset: Fraction = Fraction(0)    # added before scaling: base = (value + offset) * factor

    def to_base(self, value: Fraction) -> Fraction:
        return (value + self.offset) * self.factor

    def from_base(self, value: Fraction) -> Fraction:
        return value / self.factor - self.offset


@dataclass(frozen=True)
class Category:
    key: str
    name: str
    units: tuple[Unit, ...]
    default_from: str
    default_to: str
    negative: bool = False            # whether a negative value makes sense

    def unit(self, key: str) -> Unit:
        for unit in self.units:
            if unit.key == key:
                return unit
        raise KeyError(f"no unit {key!r} in {self.name}")

    def keys(self) -> list[str]:
        return [unit.key for unit in self.units]


def _units(*rows: tuple) -> tuple[Unit, ...]:
    return tuple(Unit(key, name, symbol, F(factor), F(offset[0]) if offset else F(0))
                 for key, name, symbol, factor, *offset in rows)


_INCH = F("0.0254")
_FOOT = _INCH * 12
_YARD = _FOOT * 3
_MILE = _FOOT * 5280
_POUND = F("0.45359237")                    # kilograms
_GRAVITY = F("9.80665")                     # standard acceleration of free fall, m/s^2
_POUND_FORCE = _POUND * _GRAVITY            # newtons
_FOOT_POUND = _FOOT * _POUND_FORCE          # joules
_BTU = F("1055.05585262")                   # International Table, joules
_US_GALLON = 231 * _INCH ** 3               # cubic metres
_UK_GALLON = F("0.00454609")
_LITRE = F(1, 1000)

LENGTH = Category("length", "Length", _units(
    ("angstrom", "Angstroms", "Å", F(1, 10 ** 10)),
    ("nanometer", "Nanometers", "nm", F(1, 10 ** 9)),
    ("micron", "Microns", "µm", F(1, 10 ** 6)),
    ("millimeter", "Millimeters", "mm", F(1, 1000)),
    ("centimeter", "Centimeters", "cm", F(1, 100)),
    ("meter", "Meters", "m", 1),
    ("kilometer", "Kilometers", "km", 1000),
    ("inch", "Inches", "in", _INCH),
    ("foot", "Feet", "ft", _FOOT),
    ("yard", "Yards", "yd", _YARD),
    ("mile", "Miles", "mi", _MILE),
    ("nautical_mile", "Nautical miles", "nmi", 1852),
), "centimeter", "inch")

WEIGHT = Category("weight", "Weight and mass", _units(
    ("carat", "Carats", "ct", F(1, 5000)),
    ("milligram", "Milligrams", "mg", F(1, 10 ** 6)),
    ("centigram", "Centigrams", "cg", F(1, 10 ** 5)),
    ("decigram", "Decigrams", "dg", F(1, 10 ** 4)),
    ("gram", "Grams", "g", F(1, 1000)),
    ("dekagram", "Dekagrams", "dag", F(1, 100)),
    ("hectogram", "Hectograms", "hg", F(1, 10)),
    ("kilogram", "Kilograms", "kg", 1),
    ("tonne", "Metric tonnes", "t", 1000),
    ("ounce", "Ounces", "oz", _POUND / 16),
    ("pound", "Pounds", "lb", _POUND),
    ("stone", "Stone", "st", _POUND * 14),
    ("short_ton", "Short tons (US)", "ton (US)", _POUND * 2000),
    ("long_ton", "Long tons (UK)", "ton (UK)", _POUND * 2240),
), "kilogram", "pound")

TEMPERATURE = Category("temperature", "Temperature", _units(
    ("celsius", "Celsius", "°C", 1, F("273.15")),
    ("fahrenheit", "Fahrenheit", "°F", F(5, 9), F("459.67")),
    ("kelvin", "Kelvin", "K", 1),
), "celsius", "fahrenheit", negative=True)

AREA = Category("area", "Area", _units(
    ("square_millimeter", "Square millimeters", "mm²", F(1, 10 ** 6)),
    ("square_centimeter", "Square centimeters", "cm²", F(1, 10 ** 4)),
    ("square_meter", "Square meters", "m²", 1),
    ("hectare", "Hectares", "ha", 10 ** 4),
    ("square_kilometer", "Square kilometers", "km²", 10 ** 6),
    ("square_inch", "Square inches", "in²", _INCH ** 2),
    ("square_foot", "Square feet", "ft²", _FOOT ** 2),
    ("square_yard", "Square yards", "yd²", _YARD ** 2),
    ("acre", "Acres", "ac", _FOOT ** 2 * 43560),
    ("square_mile", "Square miles", "mi²", _MILE ** 2),
), "square_meter", "square_foot")

VOLUME = Category("volume", "Volume", _units(
    ("milliliter", "Milliliters", "mL", _LITRE / 1000),
    ("cubic_centimeter", "Cubic centimeters", "cm³", F(1, 10 ** 6)),
    ("liter", "Liters", "L", _LITRE),
    ("cubic_meter", "Cubic meters", "m³", 1),
    ("teaspoon_us", "Teaspoons (US)", "tsp (US)", _US_GALLON / 768),
    ("tablespoon_us", "Tablespoons (US)", "tbsp (US)", _US_GALLON / 256),
    ("fluid_ounce_us", "Fluid ounces (US)", "fl oz (US)", _US_GALLON / 128),
    ("cup_us", "Cups (US)", "cup (US)", _US_GALLON / 16),
    ("pint_us", "Pints (US)", "pt (US)", _US_GALLON / 8),
    ("quart_us", "Quarts (US)", "qt (US)", _US_GALLON / 4),
    ("gallon_us", "Gallons (US)", "gal (US)", _US_GALLON),
    ("cubic_inch", "Cubic inches", "in³", _INCH ** 3),
    ("cubic_foot", "Cubic feet", "ft³", _FOOT ** 3),
    ("cubic_yard", "Cubic yards", "yd³", _YARD ** 3),
    ("teaspoon_uk", "Teaspoons (UK)", "tsp (UK)", _UK_GALLON / 768),
    ("tablespoon_uk", "Tablespoons (UK)", "tbsp (UK)", _UK_GALLON / 256),
    ("fluid_ounce_uk", "Fluid ounces (UK)", "fl oz (UK)", _UK_GALLON / 160),
    ("pint_uk", "Pints (UK)", "pt (UK)", _UK_GALLON / 8),
    ("quart_uk", "Quarts (UK)", "qt (UK)", _UK_GALLON / 4),
    ("gallon_uk", "Gallons (UK)", "gal (UK)", _UK_GALLON),
), "liter", "gallon_us")

SPEED = Category("speed", "Speed", _units(
    ("centimeter_per_second", "Centimeters per second", "cm/s", F(1, 100)),
    ("meter_per_second", "Meters per second", "m/s", 1),
    ("kilometer_per_hour", "Kilometers per hour", "km/h", F(1000, 3600)),
    ("foot_per_second", "Feet per second", "ft/s", _FOOT),
    ("mile_per_hour", "Miles per hour", "mph", _MILE / 3600),
    ("knot", "Knots", "kn", F(1852, 3600)),
    ("mach", "Mach", "M", F("340.3")),
), "kilometer_per_hour", "mile_per_hour")

TIME = Category("time", "Time", _units(
    ("microsecond", "Microseconds", "µs", F(1, 10 ** 6)),
    ("millisecond", "Milliseconds", "ms", F(1, 1000)),
    ("second", "Seconds", "s", 1),
    ("minute", "Minutes", "min", 60),
    ("hour", "Hours", "h", 3600),
    ("day", "Days", "d", 86400),
    ("week", "Weeks", "wk", 604800),
    ("year", "Years", "yr", 31557600),
), "hour", "minute")

_DATA_ROWS = [("bit", "Bits", "b", 1), ("nibble", "Nibbles", "nibble", 4), ("byte", "Bytes", "B", 8)]
for _power, (_decimal, _binary) in enumerate((("kilo", "kibi"), ("mega", "mebi"), ("giga", "gibi"),
                                              ("tera", "tebi"), ("peta", "pebi"), ("exa", "exbi"),
                                              ("zetta", "zebi"), ("yotta", "yobi")), start=1):
    _symbol = "kMGTPEZY"[_power - 1]
    _binary_symbol = _symbol.upper() + "i"
    _DATA_ROWS += [
        (f"{_decimal}bit", f"{_decimal.capitalize()}bits", f"{_symbol}b", 1000 ** _power),
        (f"{_binary}bit", f"{_binary.capitalize()}bits", f"{_binary_symbol}b", 1024 ** _power),
        (f"{_decimal}byte", f"{_decimal.capitalize()}bytes", f"{_symbol}B", 8 * 1000 ** _power),
        (f"{_binary}byte", f"{_binary.capitalize()}bytes", f"{_binary_symbol}B", 8 * 1024 ** _power),
    ]
DATA = Category("data", "Data", _units(*_DATA_ROWS), "gigabyte", "megabyte")

ENERGY = Category("energy", "Energy", _units(
    ("electron_volt", "Electron volts", "eV", F("1.602176634e-19")),
    ("joule", "Joules", "J", 1),
    ("kilojoule", "Kilojoules", "kJ", 1000),
    ("calorie", "Thermal calories", "cal", F("4.184")),
    ("food_calorie", "Food calories", "kcal", 4184),
    ("foot_pound", "Foot-pounds", "ft·lb", _FOOT_POUND),
    ("btu", "British thermal units", "BTU", _BTU),
    ("kilowatt_hour", "Kilowatt-hours", "kWh", 3600000),
), "joule", "food_calorie")

POWER = Category("power", "Power", _units(
    ("watt", "Watts", "W", 1),
    ("kilowatt", "Kilowatts", "kW", 1000),
    ("horsepower", "Horsepower (US)", "hp", _FOOT_POUND * 550),
    ("foot_pound_per_minute", "Foot-pounds/minute", "ft·lb/min", _FOOT_POUND / 60),
    ("btu_per_minute", "BTUs/minute", "BTU/min", _BTU / 60),
), "kilowatt", "horsepower")

PRESSURE = Category("pressure", "Pressure", _units(
    ("atmosphere", "Atmospheres", "atm", 101325),
    ("bar", "Bars", "bar", 100000),
    ("kilopascal", "Kilopascals", "kPa", 1000),
    ("mmhg", "Millimeters of mercury", "mmHg", F("133.322387415")),
    ("pascal", "Pascals", "Pa", 1),
    ("psi", "Pounds per square inch", "psi", _POUND_FORCE / _INCH ** 2),
), "atmosphere", "bar")

ANGLE = Category("angle", "Angle", _units(
    ("degree", "Degrees", "°", 1),
    ("radian", "Radians", "rad", 180 / numeric.PI),
    ("gradian", "Gradians", "grad", F(9, 10)),
), "degree", "radian", negative=True)

CATEGORIES: tuple[Category, ...] = (LENGTH, WEIGHT, TEMPERATURE, AREA, VOLUME, SPEED, TIME, DATA, ENERGY, POWER,
                                    PRESSURE, ANGLE)
_BY_KEY = {category.key: category for category in CATEGORIES}

MIN_SIGNIFICANT = 7                   # digits a result shows at least ...
MAX_SIGNIFICANT = 15                  # ... and at most, when that many were typed
MAX_INPUT_DIGITS = 15


def category(key: str) -> Category:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise KeyError(f"unknown category {key!r}") from None


def convert(value: Fraction, source: Unit, target: Unit) -> Fraction:
    """``value`` of ``source`` expressed in ``target``, exactly."""
    if source is target:
        return Fraction(value)
    return target.from_base(source.to_base(Fraction(value)))


def significant_digits(text: str) -> int:
    """How many significant digits a typed number has (``0.050`` has two)."""
    digits = "".join(character for character in text if character.isdigit()).lstrip("0")
    return max(1, len(digits))


def format_result(value: Fraction, typed_digits: int = 1, *, decimal_point: str = ".",
                  group_separator: str = ",") -> str:
    """A converted value for the display.

    Rounded to 7 significant digits, or to as many as were typed (up to 15),
    whichever is more; the digits before the decimal point are never
    rounded away while there are no more than 15 of them. Very large and
    very small results are written with an exponent.
    """
    if value == 0:
        return "0"
    wanted = max(MIN_SIGNIFICANT, min(MAX_SIGNIFICANT, typed_digits))
    _digits, exponent = numeric.digits_of(value, MAX_SIGNIFICANT)
    if exponent > MAX_SIGNIFICANT or exponent < -(MAX_SIGNIFICANT - MIN_SIGNIFICANT):
        return numeric.format_number(numeric.round_significant(value, wanted), wanted, numeric.SCIENTIFIC,
                                     decimal_point=decimal_point, group_separator=group_separator)
    shown = max(wanted, exponent)
    return numeric.format_number(numeric.round_significant(value, shown), MAX_SIGNIFICANT + 8, numeric.FLOAT,
                                 decimal_point=decimal_point, group_separator=group_separator)


class ConverterInput:
    """The number typed into a converter field: digits, a point and, where allowed, a sign."""

    def __init__(self) -> None:
        self.text = "0"

    def clear(self) -> None:
        self.text = "0"

    def press(self, key: str, *, allow_negative: bool = False) -> bool:
        """``0``-``9``, ``.``, ``back``, ``sign``, ``ce``. Returns False when the key changed nothing."""
        negative = self.text.startswith("-")
        body = self.text[1:] if negative else self.text
        if key == "ce":
            changed = self.text != "0"
            self.text = "0"
            return changed
        if key == "back":
            body = body[:-1] or "0"
            if body in ("0", ""):
                negative = False
                body = "0"
        elif key == "sign":
            if not allow_negative or body.strip("0.") == "":
                return False
            negative = not negative
        elif key == ".":
            if "." in body:
                return False
            body += "."
        elif len(key) == 1 and key in "0123456789":
            if sum(character.isdigit() for character in body.lstrip("0")) >= MAX_INPUT_DIGITS and body != "0":
                return False
            body = key if body == "0" else body + key
        else:
            return False
        text = ("-" if negative else "") + body
        if text == self.text:
            return False
        self.text = text
        return True

    def set_value(self, value: Fraction) -> None:
        """Take over a number (paste): shown in full when it has at most 15 digits."""
        self.text = numeric.format_number(numeric.round_significant(value, MAX_INPUT_DIGITS), MAX_INPUT_DIGITS + 8,
                                          numeric.FLOAT, group_separator="") if value else "0"
        if "e" in self.text:
            self.text = "0"

    def value(self) -> Fraction:
        text = self.text.rstrip(".")
        return Fraction(text) if text not in ("", "-") else Fraction(0)

    def display(self, decimal_point: str = ".", group_separator: str = ",") -> str:
        negative = self.text.startswith("-")
        whole, point, fraction = self.text.lstrip("-").partition(".")
        text = numeric.group_digits(whole, group_separator)
        if point:
            text += decimal_point + fraction
        return ("-" if negative else "") + text
