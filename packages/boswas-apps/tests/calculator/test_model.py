"""Engine details the table does not show, and the calculator model: memory, history, modes, copy and paste."""

from __future__ import annotations

import unittest
from fractions import Fraction as F

from boswas_apps.calculator import numeric
from boswas_apps.calculator.engine import (PROGRAMMER, SCIENTIFIC, STANDARD, DecimalInput, Engine, IntegerInput,
                                           format_integer, to_signed, unary_label)
from boswas_apps.calculator.model import MAX_HISTORY, MAX_MEMORY, Calculator


class EngineDetailTest(unittest.TestCase):
    def test_press_reports_refused_keys(self):
        engine = Engine(STANDARD)
        self.assertTrue(engine.press("5"))
        self.assertTrue(engine.press("."))
        self.assertFalse(engine.press("."))
        self.assertFalse(engine.press("("))             # no parentheses in Standard mode
        self.assertFalse(engine.press("sin"))
        self.assertFalse(engine.press("nonsense"))
        self.assertFalse(engine.press(""))
        self.assertTrue(engine.press("="))
        self.assertFalse(engine.press("back"))          # a result is not edited
        self.assertEqual(engine.display, "5")

    def test_error_state(self):
        engine = Engine(STANDARD).press_all("1 / 0 =")
        self.assertEqual(engine.error, "Cannot divide by zero")
        for key in ("5", "+", "=", "sqrt", "back", "sign", ".", "percent"):
            self.assertFalse(engine.press(key))
        self.assertEqual(engine.display, "Cannot divide by zero")
        self.assertTrue(engine.press("ce"))
        self.assertIsNone(engine.error)
        self.assertEqual(engine.display, "0")
        self.assertEqual(engine.press_all("2 + 2 =").display, "4")

    def test_parenthesis_depth(self):
        engine = Engine(SCIENTIFIC)
        depths = []
        for key in "( ( 2 + ( 3 ) ) ) )".split():
            engine.press(key)
            depths.append(engine.parenthesis_depth)
        self.assertEqual(depths, [1, 2, 2, 2, 3, 3, 2, 1, 0, 0])
        engine.press_all("c ( ( 1 + 2 =")
        self.assertEqual(engine.parenthesis_depth, 0)
        self.assertEqual(engine.display, "3")

    def test_parentheses_are_limited(self):
        engine = Engine(SCIENTIFIC)
        accepted = sum(engine.press("(") for _ in range(40))
        self.assertEqual(accepted, 25)
        self.assertEqual(engine.press_all("7 =").display, "7")
        self.assertEqual(engine.parenthesis_depth, 0)

    def test_waiting_operators_are_limited(self):
        engine = Engine(SCIENTIFIC)
        for _ in range(60):
            engine.press_all("1 + 1 * 1 pow ( ")
        # Operators that wait for a higher one share 25 places with the open parentheses.
        self.assertEqual(engine.parenthesis_depth, 8)
        self.assertFalse(engine.press("("))
        engine.press_all("1 =")
        self.assertIsNone(engine.error)
        self.assertEqual(engine.parenthesis_depth, 0)
        self.assertEqual(engine.display, "2")                  # 1 + 1 × 1 ^ (...) is 2 at every level

    def test_history_of_standard_mode_records_each_step(self):
        engine = Engine(STANDARD).press_all("2 + 3 * 4 =")
        self.assertEqual([(text, result) for text, result, _value in engine.history],
                         [("2 + 3 =", "5"), ("5 × 4 =", "20")])

    def test_history_of_scientific_mode_records_the_whole_line(self):
        engine = Engine(SCIENTIFIC).press_all("2 + 3 * 4 = 9 sqrt 5")
        self.assertEqual([(text, result) for text, result, _value in engine.history],
                         [("2 + 3 × 4 =", "14"), ("√(9) =", "3")])

    def test_angle_marks_in_the_expression(self):
        self.assertEqual(unary_label("sin", numeric.DEG), "sin₀")
        self.assertEqual(unary_label("cos", numeric.RAD), "cosᵣ")
        self.assertEqual(unary_label("tan", numeric.GRAD), "tan₉")
        self.assertEqual(unary_label("atan", numeric.RAD), "tanᵣ⁻¹")
        self.assertEqual(unary_label("acosh"), "cosh⁻¹")
        self.assertEqual(unary_label("sqrt"), "√")

    def test_angle_unit_ends_typing(self):
        engine = Engine(SCIENTIFIC).press_all("9 0")
        engine.set_angle(numeric.RAD)
        self.assertFalse(engine.recording)
        engine.set_angle(numeric.DEG)
        self.assertEqual(engine.press_all("sin").display, "1")
        with self.assertRaises(ValueError):
            engine.set_angle("turns")

    def test_locale_marks_reach_the_display(self):
        engine = Engine(STANDARD, decimal_point=",", group_separator=".")
        engine.press_all("1234567.5")
        self.assertEqual(engine.display, "1.234.567,5")
        self.assertEqual(engine.press_all("* 2 =").display, "2.469.135")
        self.assertEqual(engine.press_all("/ 4 =").display, "617.283,75")

    def test_rand(self):
        engine = Engine(SCIENTIFIC, rng=lambda: 0.25)
        self.assertEqual(engine.press_all("rand").display, "0.25")
        self.assertEqual(engine.press_all("* 4 =").display, "1")
        value = Engine(SCIENTIFIC).press_all("rand").value
        self.assertTrue(0 <= value < 1)

    def test_value_follows_typing(self):
        engine = Engine(STANDARD).press_all("12.5")
        self.assertEqual(engine.value, F(25, 2))
        self.assertEqual(engine.commit(), F(25, 2))
        self.assertFalse(engine.recording)

    def test_recall_acts_like_a_typed_number(self):
        engine = Engine(STANDARD).press_all("5 +")
        engine.recall(F(7))
        self.assertEqual(engine.press_all("=").display, "12")
        engine.recall(F(3))
        self.assertEqual(engine.display, "3")
        self.assertEqual(engine.press_all("4").display, "4")

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            Engine("graphing")


class InputTest(unittest.TestCase):
    def test_decimal_input(self):
        entry = DecimalInput(16)
        for digit in (1, 2):
            self.assertTrue(entry.add_digit(digit))
        self.assertTrue(entry.add_point())
        self.assertFalse(entry.add_point())
        self.assertTrue(entry.add_digit(5))
        self.assertEqual(entry.value(), F("12.5"))
        self.assertTrue(entry.begin_exponent())
        self.assertFalse(entry.begin_exponent())
        self.assertFalse(entry.add_point())
        entry.add_digit(2)
        entry.toggle_sign()
        self.assertEqual(entry.value(), F("0.125"))
        self.assertEqual(entry.text(".", ","), "12.5e-2")
        entry.backspace()
        entry.backspace()
        self.assertEqual(entry.text(".", ","), "12.5")
        self.assertFalse(entry.add_digit(10))

    def test_exponent_has_four_digits(self):
        entry = DecimalInput(32)
        entry.add_digit(1)
        entry.begin_exponent()
        results = [entry.add_digit(9) for _ in range(6)]
        self.assertEqual(results, [True, True, True, True, False, False])
        self.assertEqual(entry.value(), F(10) ** 9999)

    def test_integer_input_limits(self):
        cases = ((10, 8, "127", "128"), (10, 16, "32767", "32768"), (16, 8, "FF", "100"), (8, 8, "377", "400"),
                 (2, 8, "11111111", "100000000"), (16, 64, "F" * 16, "1" + "0" * 16), (8, 16, "177777", "200000"))
        for radix, bits, fits, too_big in cases:
            with self.subTest(radix=radix, bits=bits):
                entry = IntegerInput(radix, bits)
                self.assertTrue(all(entry.add_digit(int(digit, 16)) for digit in fits))
                entry.clear()
                self.assertFalse(all(entry.add_digit(int(digit, 16)) for digit in too_big))

    def test_integer_helpers(self):
        self.assertEqual(to_signed(0xFF, 8), -1)
        self.assertEqual(to_signed(0x7F, 8), 127)
        self.assertEqual(to_signed(0x8000, 16), -32768)
        self.assertEqual(format_integer(0xFF, 10, 8), "-1")
        self.assertEqual(format_integer(0xFF, 16, 8), "FF")
        self.assertEqual(format_integer(0xFF, 2, 8), "1111 1111")
        self.assertEqual(format_integer(0xFF, 8, 8), "377")
        self.assertEqual(format_integer(0xFFFF, 16, 16, grouped=False), "FFFF")
        self.assertEqual(format_integer(1234567, 10, 32), "1,234,567")
        self.assertEqual(format_integer(1234567, 10, 32, "."), "1.234.567")


class ProgrammerTest(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(PROGRAMMER)

    def test_all_four_radixes(self):
        self.engine.press_all("255")
        self.assertEqual([self.engine.display_in(radix) for radix in (16, 10, 8, 2)],
                         ["FF", "255", "377", "1111 1111"])
        self.engine.set_radix(16)
        self.assertEqual(self.engine.display, "FF")
        self.assertEqual(self.engine.press_all("+ 1 =").display, "100")
        self.engine.set_radix(2)
        self.assertEqual(self.engine.display, "1 0000 0000")
        self.engine.set_radix(10)
        self.assertEqual(self.engine.display, "256")
        with self.assertRaises(ValueError):
            self.engine.set_radix(3)

    def test_digit_keys_follow_the_radix(self):
        self.assertFalse(self.engine.accepts("A"))
        self.assertTrue(self.engine.accepts("9"))
        self.engine.set_radix(16)
        self.assertTrue(all(self.engine.accepts(key) for key in "ABCDEF"))
        self.engine.set_radix(8)
        self.assertFalse(self.engine.accepts("8"))
        self.assertTrue(self.engine.accepts("7"))
        self.engine.set_radix(2)
        self.assertFalse(self.engine.accepts("2"))
        self.assertFalse(self.engine.accepts("."))
        self.assertFalse(self.engine.accepts("sin"))

    def test_word_size_down_keeps_the_low_bits(self):
        self.engine.press_all("511")
        self.engine.set_word_size(8)
        self.assertEqual(self.engine.display, "-1")
        self.engine.press_all("c 300")
        self.assertEqual(self.engine.display, "30")           # 300 does not fit a byte: the third digit is refused
        engine = Engine(PROGRAMMER).press_all("300")
        engine.set_word_size(8)
        self.assertEqual(engine.display, "44")

    def test_word_size_up_extends_the_sign(self):
        self.engine.set_word_size(8)
        self.engine.press_all("127 + 1 =")
        self.assertEqual(self.engine.display, "-128")
        self.engine.set_word_size(16)
        self.assertEqual(self.engine.display, "-128")
        self.assertEqual(self.engine.display_in(16), "FF80")
        self.engine.set_word_size(64)
        self.assertEqual(self.engine.display_in(16), "FFFF FFFF FFFF FF80")
        with self.assertRaises(ValueError):
            self.engine.set_word_size(24)

    def test_bit_keypad(self):
        self.engine.toggle_bit(3)
        self.assertEqual(self.engine.display, "8")
        self.engine.toggle_bit(0)
        self.assertEqual(self.engine.display, "9")
        self.engine.toggle_bit(3)
        self.assertEqual(self.engine.display, "1")
        self.engine.toggle_bit(63)
        self.assertEqual(self.engine.display, "-9,223,372,036,854,775,807")
        self.engine.toggle_bit(64)                             # no such bit
        self.engine.toggle_bit(-1)
        self.assertEqual(self.engine.display, "-9,223,372,036,854,775,807")
        self.engine.set_word_size(8)
        self.engine.press("c")
        self.engine.toggle_bit(7)
        self.assertEqual(self.engine.display, "-128")
        self.engine.toggle_bit(8)                              # outside a byte
        self.assertEqual(self.engine.display, "-128")

    def test_a_toggled_bit_is_an_operand(self):
        self.engine.press_all("4 +")
        self.engine.toggle_bit(0)                              # 4 with bit 0 set: 5
        self.assertEqual(self.engine.press_all("=").display, "9")

    def test_rotate_through_carry(self):
        self.engine.set_word_size(8)
        self.engine.set_radix(2)
        self.engine.press_all("10000001 rolc")
        self.assertEqual((self.engine.display, self.engine.carry), ("10", 1))
        self.engine.press("rolc")
        self.assertEqual((self.engine.display, self.engine.carry), ("101", 0))
        self.engine.press_all("c")
        self.assertEqual(self.engine.carry, 0)

    def test_typing_in_a_radix_then_changing_it(self):
        self.engine.set_radix(16)
        self.engine.press_all("1F")
        self.engine.set_radix(10)
        self.assertEqual(self.engine.display, "31")
        self.assertEqual(self.engine.press_all("+ 1 =").display, "32")


class ModelTest(unittest.TestCase):
    def setUp(self):
        self.calc = Calculator()

    def keys(self, text: str) -> str:
        self.calc.engine.press_all(text)
        return self.calc.engine.display

    def test_memory_keys(self):
        self.keys("5")
        self.assertTrue(self.calc.memory_store())
        self.assertEqual(self.keys("3"), "3")                  # MS ends the number: 3 starts a new one
        self.assertTrue(self.calc.memory_add())
        self.assertEqual(self.calc.memory_text(0), "8")
        self.keys("c 10")
        self.assertTrue(self.calc.memory_subtract())
        self.assertEqual(self.calc.memory_text(0), "-2")
        self.keys("c 7 +")
        self.assertTrue(self.calc.memory_recall())
        self.assertEqual(self.keys("="), "5")
        self.calc.memory_clear()
        self.assertEqual(self.calc.memory, [])
        self.assertFalse(self.calc.memory_recall())

    def test_memory_plus_and_minus_start_a_memory(self):
        self.keys("4")
        self.assertTrue(self.calc.memory_subtract())
        self.assertEqual(self.calc.memory_text(0), "-4")
        self.calc.memory_clear()
        self.assertTrue(self.calc.memory_add())
        self.assertEqual(self.calc.memory_text(0), "4")

    def test_memory_is_a_list(self):
        for number in ("1", "2", "3"):
            self.keys(f"c {number}")
            self.calc.memory_store()
        self.assertEqual([self.calc.memory_text(i) for i in range(3)], ["3", "2", "1"])
        self.keys("c 10")
        self.calc.memory_add(2)
        self.calc.memory_subtract(1)
        self.assertEqual([self.calc.memory_text(i) for i in range(3)], ["3", "-8", "11"])
        self.calc.memory_recall(2)
        self.assertEqual(self.calc.engine.display, "11")
        self.calc.memory_clear(0)
        self.assertEqual([self.calc.memory_text(i) for i in range(2)], ["-8", "11"])
        self.assertFalse(self.calc.memory_add(5))
        self.calc.memory_clear(9)                              # no such entry: nothing happens
        self.assertEqual(len(self.calc.memory), 2)

    def test_memory_is_capped(self):
        for _ in range(MAX_MEMORY + 20):
            self.calc.memory_store()
        self.assertEqual(len(self.calc.memory), MAX_MEMORY)

    def test_memory_does_nothing_in_an_error_state(self):
        self.keys("1 / 0 =")
        self.assertFalse(self.calc.memory_store())
        self.assertFalse(self.calc.memory_add())
        self.assertEqual(self.calc.memory, [])

    def test_memory_keeps_full_precision(self):
        self.keys("1 / 3 =")
        self.calc.memory_store()
        self.keys("c")
        self.calc.memory_recall()
        self.assertEqual(self.keys("* 3 ="), "1")

    def test_history(self):
        self.keys("2 + 3 = 4 * 5 =")
        self.assertEqual([(item.expression, item.result) for item in self.calc.history],
                         [("4 × 5 =", "20"), ("2 + 3 =", "5")])
        self.assertTrue(self.calc.recall_history(1))
        self.assertEqual(self.calc.engine.display, "5")
        self.assertEqual(self.keys("+ 1 ="), "6")
        self.assertFalse(self.calc.recall_history(99))
        self.calc.clear_history()
        self.assertEqual(self.calc.history, [])

    def test_history_is_capped_and_kept_per_mode(self):
        for number in range(MAX_HISTORY + 5):
            self.keys(f"c {number} + 1 =")
        self.assertEqual(len(self.calc.history), MAX_HISTORY)
        self.assertEqual(self.calc.history[0].result, str(MAX_HISTORY + 5))
        self.calc.set_mode(SCIENTIFIC)
        self.assertEqual(self.calc.history, [])
        self.keys("2 pow 3 =")
        self.assertEqual(len(self.calc.history), 1)
        self.calc.set_mode(PROGRAMMER)
        self.assertFalse(self.calc.has_history())
        self.assertEqual(self.calc.history, [])
        self.keys("1 + 1 =")
        self.calc.set_mode(STANDARD)
        self.assertEqual(len(self.calc.history), MAX_HISTORY)

    def test_switching_the_mode_starts_a_new_calculation_and_keeps_memory(self):
        self.keys("5 + 3")
        self.calc.memory_store()
        self.calc.set_mode(SCIENTIFIC)
        self.assertEqual(self.calc.engine.display, "0")
        self.calc.memory_recall()
        self.assertEqual(self.calc.engine.display, "3")
        self.calc.set_mode(STANDARD)
        self.assertEqual(self.calc.engine.display, "0")
        self.assertEqual(self.keys("2 ="), "2")
        with self.assertRaises(ValueError):
            self.calc.set_mode("date")

    def test_programmer_memory_is_separate_and_signed(self):
        self.keys("9")
        self.calc.memory_store()
        self.calc.set_mode(PROGRAMMER)
        self.assertEqual(self.calc.memory, [])
        self.calc.engine.set_word_size(8)
        self.keys("100")
        self.calc.memory_store()
        self.calc.memory_add()
        self.assertEqual(self.calc.memory_text(0), "-56")       # 200 wraps in a byte
        self.keys("c 5 sign")
        self.calc.memory_store()
        self.calc.engine.set_radix(16)
        self.assertEqual(self.calc.memory_text(0), "FB")
        self.calc.memory_recall(1)
        self.assertEqual(self.calc.engine.display, "C8")

    def test_copy(self):
        self.keys("1234.5 * 2 =")
        self.assertEqual(self.calc.copy_text(), "2,469")

    def test_paste_numbers(self):
        for text, shown in (("42", "42"), (" 1,234.5\n", "1,234.5"), ("-7", "-7"), ("1e3", "1,000"),
                            ("0.000001", "0.000001")):
            with self.subTest(text=text):
                calc = Calculator()
                self.assertTrue(calc.paste(text))
                self.assertEqual(calc.engine.display, shown)

    def test_paste_is_an_operand(self):
        self.keys("5 *")
        self.assertTrue(self.calc.paste("4"))
        self.assertEqual(self.keys("="), "20")

    def test_paste_of_something_else_is_invalid_input(self):
        for text in ("hello", "", "1+1", "12abc", "0x1F", "\x00", "9" * 1000, "1e99999"):
            with self.subTest(text=text):
                calc = Calculator()
                self.assertFalse(calc.paste(text))
                self.assertEqual(calc.engine.display, "Invalid input")
                calc.press("c")
                self.assertEqual(calc.engine.display, "0")

    def test_paste_in_programmer_mode(self):
        self.calc.set_mode(PROGRAMMER)
        engine = self.calc.engine
        self.assertTrue(self.calc.paste("-12"))
        self.assertEqual(engine.display, "-12")
        self.assertFalse(self.calc.paste("1.5"))
        engine.press("c")
        self.assertFalse(self.calc.paste("9223372036854775808"))
        engine.press("c")
        self.assertTrue(self.calc.paste("-9223372036854775808"))
        engine.set_radix(16)
        self.assertTrue(self.calc.paste("0xFF"))
        self.assertEqual(engine.display, "FF")
        self.assertTrue(self.calc.paste("dead beef"))
        self.assertEqual(engine.display, "DEAD BEEF")
        self.assertFalse(self.calc.paste("-1"))
        engine.press("c")
        self.assertFalse(self.calc.paste("1" + "0" * 16))
        engine.press("c")
        engine.set_radix(2)
        self.assertTrue(self.calc.paste("0b1010"))
        self.assertEqual(engine.display, "1010")
        self.assertFalse(self.calc.paste("102"))
        engine.press("c")
        engine.set_word_size(8)
        engine.set_radix(10)
        self.assertFalse(self.calc.paste("128"))
        engine.press("c")
        self.assertTrue(self.calc.paste("-128"))

    def test_locale(self):
        calc = Calculator(decimal_point=",", group_separator=".")
        calc.engine.press_all("1 / 4 =")
        self.assertEqual(calc.engine.display, "0,25")
        self.assertTrue(calc.paste("1.234,5"))
        self.assertEqual(calc.engine.display, "1.234,5")
        calc.memory_store()
        self.assertEqual(calc.memory_text(0), "1.234,5")


if __name__ == "__main__":
    unittest.main()
