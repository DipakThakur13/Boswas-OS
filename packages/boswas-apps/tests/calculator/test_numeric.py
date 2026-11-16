"""The numbers of the calculator: exactness, rounding, the display format, hostile input."""

from __future__ import annotations

import time
import unittest
from fractions import Fraction as F

from boswas_apps.calculator import numeric as n


def fmt(value, precision=32, notation=n.FLOAT, **options):
    return n.format_number(F(value), precision, notation, **options)


class FormatTest(unittest.TestCase):
    def test_plain_numbers(self):
        for value, expected in ((0, "0"), (1, "1"), (-1, "-1"), ("0.5", "0.5"), ("-0.5", "-0.5"), (1234, "1,234"),
                                (1234567, "1,234,567"), ("1234.5", "1,234.5"), ("0.001", "0.001"), (999, "999"),
                                (1000, "1,000"), ("-1234567.125", "-1,234,567.125"), ("0.000001", "0.000001")):
            with self.subTest(value=value):
                self.assertEqual(fmt(value), expected)

    def test_sixteen_digit_display(self):
        self.assertEqual(fmt(F(1, 3), 16), "0.3333333333333333")
        self.assertEqual(fmt(F(2, 3), 16), "0.6666666666666667")
        self.assertEqual(fmt(F(1, 30), 16), "0.0" + "3" * 15)
        self.assertEqual(fmt(F(1, 300), 16), "0.00" + "3" * 14)
        self.assertEqual(fmt(F(1, 3000), 16), "3.333333333333333e-4")
        self.assertEqual(fmt(F(2, 3000), 16), "6.666666666666667e-4")
        self.assertEqual(fmt(10 ** 16, 16), "1.e+16")
        self.assertEqual(fmt(10 ** 15, 16), "1,000,000,000,000,000")
        self.assertEqual(fmt(9999999999999999, 16), "9,999,999,999,999,999")
        self.assertEqual(fmt(F(1, 10 ** 17), 16), "1.e-17")
        self.assertEqual(fmt(F(1, 10 ** 15), 16), "0.000000000000001")
        self.assertEqual(fmt(F(1, 10 ** 16), 16), "0.0000000000000001")

    def test_thirty_two_digit_display(self):
        self.assertEqual(fmt(F(1, 3)), "0." + "3" * 32)
        self.assertEqual(fmt(F(2, 3)), "0." + "6" * 31 + "7")
        self.assertEqual(fmt(F(1, 30)), "0.0" + "3" * 31)
        self.assertEqual(fmt(10 ** 32), "1.e+32")
        self.assertEqual(fmt(10 ** 31), "10," + ",".join(["000"] * 10))
        self.assertEqual(fmt(F(1, 3000)), "3." + "3" * 31 + "e-4")

    def test_rounding_is_half_away_from_zero_and_can_carry(self):
        self.assertEqual(fmt("0.15", 1), "0.2")
        self.assertEqual(fmt("-0.15", 1), "-0.2")
        self.assertEqual(fmt("0.25", 1), "0.3")
        self.assertEqual(fmt("9.99", 2), "10")
        self.assertEqual(fmt("0.0999", 2), "0.1")
        self.assertEqual(fmt("0.0999", 16), "0.0999")
        self.assertEqual(fmt("99999999999999995", 16), "1.e+17")
        self.assertEqual(fmt(F("0.09999999999999999999"), 16), "0.1")
        self.assertEqual(fmt(F("0.009999999999999999999"), 16), "0.01")

    def test_scientific_notation(self):
        self.assertEqual(fmt(0, 32, n.SCIENTIFIC), "0.e+0")
        self.assertEqual(fmt(1000, 32, n.SCIENTIFIC), "1.e+3")
        self.assertEqual(fmt("1234.5", 32, n.SCIENTIFIC), "1.2345e+3")
        self.assertEqual(fmt("-0.05", 32, n.SCIENTIFIC), "-5.e-2")
        self.assertEqual(fmt(1, 32, n.SCIENTIFIC), "1.e+0")

    def test_locale_marks(self):
        self.assertEqual(fmt("1234567.5", decimal_point=",", group_separator="."), "1.234.567,5")
        self.assertEqual(fmt("1234567.5", decimal_point=",", group_separator=" "), "1 234 567,5")
        self.assertEqual(fmt("1234567.5", group_separator=""), "1234567.5")
        self.assertEqual(fmt(F(1, 3000), 16, decimal_point=","), "3,333333333333333e-4")

    def test_group_digits(self):
        self.assertEqual(n.group_digits("1", ","), "1")
        self.assertEqual(n.group_digits("123", ","), "123")
        self.assertEqual(n.group_digits("1234", ","), "1,234")
        self.assertEqual(n.group_digits("123456", ","), "123,456")
        self.assertEqual(n.group_digits("11110000", " ", 4), "1111 0000")
        self.assertEqual(n.group_digits("1234", ""), "1234")

    def test_huge_numbers_do_not_need_text_conversion_of_the_integer(self):
        # CPython refuses str() of an integer of more than 4300 digits; the formatter must not need it.
        value = F(10) ** 9999 * 7
        self.assertEqual(fmt(value), "7.e+9999")
        self.assertEqual(fmt(1 / value), "1.4285714285714285714285714285714e-10000")
        self.assertEqual(n.log10(F(10) ** 9000), 9000)


class ParseTest(unittest.TestCase):
    def test_numbers(self):
        for text, expected in (("5", F(5)), (" 12.5 ", F(25, 2)), ("-3", F(-3)), ("1,234.5", F(2469, 2)),
                               ("1e3", F(1000)), ("1.e+3", F(1000)), ("2.5E-3", F(1, 400)), ("−7", F(-7)),
                               ("+4", F(4)), (".5", F(1, 2)), ("5.", F(5)), ("0", F(0)), ("-0", F(0)),
                               ("1 234", F(1234))):
            with self.subTest(text=text):
                self.assertEqual(n.parse_number(text), expected)

    def test_other_locale(self):
        self.assertEqual(n.parse_number("1.234,5", decimal_point=",", group_separator="."), F(2469, 2))
        self.assertEqual(n.parse_number("1 234,5", decimal_point=",", group_separator=" "), F(2469, 2))
        self.assertIsNone(n.parse_number("1.5", decimal_point=",", group_separator=""))

    def test_refused(self):
        for text in ("", " ", "abc", "1.2.3", "1e", "e5", "--1", "1-", "0x10", "1e999999", "1/2", "٣", "１２",
                     "1e+", ".", "-", "5 + 5", "=1+1", "\n", "9" * 500, "1e10000", None, 5, b"5"):
            with self.subTest(text=text):
                self.assertIsNone(n.parse_number(text))


class ExactnessTest(unittest.TestCase):
    def test_rational_arithmetic_is_exact(self):
        self.assertEqual(n.add(F("0.1"), F("0.2")), F("0.3"))
        self.assertEqual(n.multiply(n.divide(F(1), F(3)), F(3)), 1)
        self.assertEqual(n.multiply(n.divide(F(1), F(7)), F(7)), 1)
        self.assertEqual(n.subtract(F("1.1"), F("1")), F("0.1"))

    def test_roots_come_back(self):
        for number in (2, 3, 5, 7, 10, 123456789):
            with self.subTest(number=number):
                root = n.sqrt(F(number))
                self.assertEqual(n.multiply(root, root), number)
                cube = n.cube_root(F(number))
                self.assertEqual(n.power(cube, F(3)), number)

    def test_perfect_powers_are_exact(self):
        self.assertEqual(n.sqrt(F(4)), 2)
        self.assertEqual(n.sqrt(F(1, 4)), F(1, 2))
        self.assertEqual(n.sqrt(F("2.25")), F("1.5"))
        self.assertEqual(n.sqrt(F(10 ** 40)), 10 ** 20)
        self.assertEqual(n.power(F(27), F(2, 3)), 9)
        self.assertEqual(n.power(F(-8), F(1, 3)), -2)
        self.assertEqual(n.power(F(-8), F(2, 3)), 4)
        self.assertEqual(n.root(F(1024), F(10)), 2)
        self.assertEqual(n.power(F(2), F(-2)), F(1, 4))
        self.assertEqual(n.power(F(0), F(5)), 0)
        self.assertEqual(n.power(F(0), F(0)), 1)

    def test_logarithms_and_exponentials_are_inverse(self):
        for number in (2, 5, 10, 100, F("0.3"), F("0.001"), 12345):
            with self.subTest(number=number):
                self.assertEqual(n.exp(n.ln(F(number))), number)
                self.assertEqual(n.power(F(10), n.log10(F(number))), number)
        # A number that is not a short decimal comes back to all the digits the display shows.
        self.assertEqual(n.format_number(n.exp(n.ln(F(1, 3)))), n.format_number(F(1, 3)))
        self.assertEqual(n.ln(n.E), 1)
        self.assertEqual(n.log10(F(1000)), 3)
        self.assertEqual(n.log10(F(1, 1000)), -3)
        self.assertEqual(n.log_base(F(8), F(2)), 3)
        self.assertEqual(n.log_base(F(1, 8), F(2)), -3)

    def test_special_angles(self):
        self.assertEqual(n.sin(F(180), n.DEG), 0)
        self.assertEqual(n.sin(F(30), n.DEG), F(1, 2))
        self.assertEqual(n.cos(F(60), n.DEG), F(1, 2))
        self.assertEqual(n.cos(F(90), n.DEG), 0)
        self.assertEqual(n.tan(F(45), n.DEG), 1)
        self.assertEqual(n.sin(F(-270), n.DEG), 1)
        self.assertEqual(n.sin(F(36000030), n.DEG), F(1, 2))
        self.assertEqual(n.sin(F(100), n.GRAD), 1)
        self.assertEqual(n.cos(F(200), n.GRAD), -1)
        self.assertEqual(n.sin(n.PI, n.RAD), 0)
        self.assertEqual(n.cos(n.PI, n.RAD), -1)
        self.assertEqual(n.sin(n.PI / 6, n.RAD), F(1, 2))
        self.assertEqual(n.cos(n.PI / 2, n.RAD), 0)
        self.assertEqual(n.tan(n.PI / 4, n.RAD), 1)
        self.assertEqual(n.sin(n.PI * 1000, n.RAD), 0)
        self.assertEqual(n.asin(F(1, 2), n.DEG), 30)
        self.assertEqual(n.acos(F(1, 2), n.DEG), 60)
        self.assertEqual(n.atan(F(1), n.DEG), 45)
        self.assertEqual(n.atan(F(1), n.RAD) * 4, n.PI)
        self.assertEqual(n.asin(F(1), n.RAD) * 2, n.PI)
        self.assertEqual(n.acos(F(-1), n.RAD), n.PI)

    def test_inverse_functions_undo_the_functions(self):
        for value in (F("0.3"), F("-0.75"), F("0.999999"), F(1, 8)):
            with self.subTest(value=value):
                for unit in n.ANGLE_UNITS:
                    self.assertEqual(n.sin(n.asin(value, unit), unit), value)
                    self.assertEqual(n.cos(n.acos(value, unit), unit), value)
                    self.assertEqual(n.tan(n.atan(value, unit), unit), value)
                self.assertEqual(n.sinh(n.asinh(value)), value)
                self.assertEqual(n.tanh(n.atanh(value)), value)
                self.assertEqual(n.cosh(n.acosh(abs(value) + 1)), abs(value) + 1)

    def test_pythagoras_holds_for_any_angle(self):
        for degrees in (1, 17, 33, 123, 359, F(1, 7)):
            with self.subTest(degrees=degrees):
                sine, cosine = n.sin(F(degrees), n.DEG), n.cos(F(degrees), n.DEG)
                self.assertEqual(n.add(n.multiply(sine, sine), n.multiply(cosine, cosine)), 1)

    def test_a_tiny_argument_is_not_snapped_away(self):
        tiny = F(1, 10 ** 45)
        self.assertEqual(n.sin(tiny, n.RAD), tiny)
        self.assertEqual(n.sinh(tiny), tiny)
        self.assertEqual(n.tanh(tiny), tiny)
        self.assertEqual(n.atanh(tiny), tiny)
        self.assertEqual(n.asinh(tiny), tiny)

    def test_known_values_to_32_digits(self):
        known = {
            "pi": (n.PI, "3.1415926535897932384626433832795"),
            "e": (n.E, "2.7182818284590452353602874713527"),
            "sqrt 2": (n.sqrt(F(2)), "1.4142135623730950488016887242097"),
            "sqrt 3": (n.sqrt(F(3)), "1.7320508075688772935274463415059"),
            "ln 2": (n.ln(F(2)), "0.69314718055994530941723212145818"),
            "ln 10": (n.ln(F(10)), "2.3025850929940456840179914546844"),
            "log 2": (n.log10(F(2)), "0.30102999566398119521373889472449"),
            "e^pi": (n.exp(n.PI), "23.140692632779269005729086367949"),
            "sin 1": (n.sin(F(1)), "0.8414709848078965066525023216303"),
            "cos 1": (n.cos(F(1)), "0.54030230586813971740093660744298"),
            "tan 1": (n.tan(F(1)), "1.5574077246549022305069748074584"),
            "atan 2": (n.atan(F(2)), "1.1071487177940905030170654601785"),
            "acos 0.3": (n.acos(F("0.3")), "1.2661036727794991112593187304122"),
            "sinh 1": (n.sinh(F(1)), "1.1752011936438014568823818505956"),
            "cosh 1": (n.cosh(F(1)), "1.5430806348152437784779056207571"),
            "tanh 1": (n.tanh(F(1)), "0.76159415595576488811945828260479"),
            "asinh 1": (n.asinh(F(1)), "0.88137358701954302523260932497979"),
            "acosh 2": (n.acosh(F(2)), "1.316957896924816708625046347308"),
            "atanh 0.5": (n.atanh(F(1, 2)), "0.54930614433405484569762261846126"),
            "0.5!": (n.factorial(F(1, 2)), "0.88622692545275801364908374167057"),
            "(-1.5)!": (n.factorial(F(-3, 2)), "-3.5449077018110320545963349666823"),
            "2^0.5": (n.power(F(2), F(1, 2)), "1.4142135623730950488016887242097"),
            "2^(1/3)": (n.cube_root(F(2)), "1.2599210498948731647672106072782"),
            "sin 1e50": (n.sin(F(10) ** 50), "-0.78967249342931008271028953991741"),
        }
        for name, (value, expected) in known.items():
            with self.subTest(name=name):
                self.assertEqual(n.format_number(value), expected)

    def test_gamma_agrees_with_the_factorial_next_to_whole_numbers(self):
        for whole in (1, 5, 20, 100):
            with self.subTest(whole=whole):
                near = n.factorial(F(whole) + F(1, 10 ** 30))
                exact = n.factorial(F(whole))
                self.assertLess(abs(near / exact - 1), F(1, 10 ** 27))
        self.assertEqual(n.format_number(n.factorial(F("3.5"))), "11.631728396567448929144224109426")

    def test_dms(self):
        self.assertEqual(n.to_dms(F("2.5")), F("2.3"))
        self.assertEqual(n.from_dms(F("2.3")), F("2.5"))
        self.assertEqual(n.to_dms(F("-2.5")), F("-2.3"))
        self.assertEqual(n.from_dms(F("30.2630")), F(30) + F(26, 60) + F(30, 3600))
        self.assertEqual(n.from_dms(n.to_dms(F("12.3456"))), F("12.3456"))


class ErrorTest(unittest.TestCase):
    def assert_error(self, code, function, *arguments):
        with self.assertRaises(n.CalcError) as caught:
            function(*arguments)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.message, n.MESSAGES[code])

    def test_division(self):
        self.assert_error(n.DIVIDE_BY_ZERO, n.divide, F(1), F(0))
        self.assert_error(n.UNDEFINED, n.divide, F(0), F(0))
        self.assert_error(n.UNDEFINED, n.modulo, F(1), F(0))
        self.assert_error(n.DIVIDE_BY_ZERO, n.reciprocal, F(0))

    def test_domain(self):
        for function, arguments in ((n.sqrt, (F(-1),)), (n.ln, (F(0),)), (n.ln, (F(-1),)), (n.log10, (F(0),)),
                                    (n.asin, (F(2),)), (n.acos, (F(-2),)), (n.acosh, (F(0),)), (n.atanh, (F(1),)),
                                    (n.atanh, (F(-1),)), (n.factorial, (F(-1),)), (n.tan, (F(90), n.DEG)),
                                    (n.power, (F(-2), F(1, 2))), (n.sin, (F(10) ** 100,)), (n.asec, (F(0),)),
                                    (n.log_base, (F(-8), F(2))), (n.acoth, (F(1, 2),)), (n.asech, (F(2),))):
            with self.subTest(function=function.__name__, arguments=arguments):
                self.assert_error(n.DOMAIN, function, *arguments)

    def test_overflow_at_ten_to_the_ten_thousand(self):
        self.assert_error(n.OVERFLOW, n.power, F(10), F(10000))
        self.assert_error(n.OVERFLOW, n.exp, F(23100))
        self.assert_error(n.OVERFLOW, n.factorial, F(3249))
        self.assert_error(n.OVERFLOW, n.multiply, F(10) ** 9999, F(10))
        self.assert_error(n.OVERFLOW, n.power, F(2), F(10) ** 30)
        self.assert_error(n.OVERFLOW, n.sinh, F(100000))
        self.assert_error(n.OVERFLOW, n.power, F("1.5"), F("123456.7"))
        self.assertEqual(n.format_number(n.power(F(10), F(9999))), "1.e+9999")
        self.assertEqual(n.normalize(F(1, 10 ** 10001)), 0)
        self.assertEqual(n.exp(F(-100000)), 0)
        self.assertEqual(n.power(F(2), -F(10) ** 30), 0)

    def test_huge_exponents_answer_at_once(self):
        started = time.monotonic()
        for base, exponent in ((F(3), F(10) ** 20), (F("1.0000001"), F(10 ** 9)), (F(7, 3), F(123456789)),
                               (F("1.0000001"), F("123456789.5")), (F(2), F(1, 10 ** 30))):
            try:
                n.power(base, exponent)
            except n.CalcError:
                pass
        n.factorial(F("3248.5") - 1)
        n.sin(F(10) ** 99 + F(1, 3))
        n.power(F("1.000001"), F(1000000))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_long_calculation_stays_bounded(self):
        value = F(1)
        for step in range(1, 400):
            value = n.add(n.divide(value, F(step)), F(1, step + 1))
            value = n.multiply(value, F(3, 7))
        self.assertLess(value.denominator.bit_length(), 400)
        self.assertLess(value.numerator.bit_length(), 400)


class NormalizeTest(unittest.TestCase):
    def test_simple_fractions_stay_exact(self):
        for value in (F(1, 3), F(2, 7), F(22, 7), F(1, 10 ** 30), F(10 ** 33) + 1, F(-5, 9)):
            self.assertEqual(n.normalize(value), value)

    def test_noise_is_snapped_to_the_round_number(self):
        self.assertEqual(n.normalize(F(2) + F(1, 10 ** 41)), 2)
        self.assertEqual(n.normalize(F(1) - F(1, 10 ** 40)), 1)
        self.assertEqual(n.normalize(F("0.5") + F(3, 10 ** 42)), F("0.5"))
        self.assertNotEqual(n.normalize(F(2) + F(1, 10 ** 30)), 2)

    def test_cancellation(self):
        self.assertEqual(n.subtract(F(1) + F(1, 10 ** 30), F(1)), F(1, 10 ** 30))
        self.assertEqual(n.subtract(n.multiply(n.sqrt(F(2)), n.sqrt(F(2))), F(2)), 0)
        self.assertEqual(n.subtract(n.asin(F(1, 2)) * 6, n.PI), 0)

    def test_round_significant(self):
        self.assertEqual(n.round_significant(F(123456), 3), 123000)
        self.assertEqual(n.round_significant(F(-123456), 3), -123000)
        self.assertEqual(n.round_significant(F("0.0012345"), 2), F("0.0012"))
        self.assertEqual(n.round_significant(F(995), 2), 1000)
        self.assertEqual(n.round_significant(F(0), 5), 0)
        self.assertEqual(n.digits_of(F(1, 3), 5), ("33333", 0))
        self.assertEqual(n.digits_of(F(1000), 5), ("1", 4))
        self.assertEqual(n.digits_of(F("0.05"), 5), ("5", -1))


if __name__ == "__main__":
    unittest.main()
