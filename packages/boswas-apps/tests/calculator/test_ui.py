"""The calculator window, driven as a person drives it: clicks on the keys, the keyboard, the shortcuts.

Offscreen; the remembered state goes to a temporary directory, the clipboard is Qt's own.
"""

from __future__ import annotations

import json
import re
import unittest
from datetime import date
from pathlib import Path

from boswas_fluent import testing  # selects the offscreen platform: before any other Qt import

from PySide6.QtCore import QDate, QLocale, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import boswas_fluent as fluent  # noqa: E402
from boswas_apps.calculator import APP_ID, state  # noqa: E402
from boswas_apps.calculator.ui.panel import HISTORY, MEMORY  # noqa: E402
from boswas_apps.calculator.ui.window import CalculatorWindow  # noqa: E402

CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
ALT = Qt.KeyboardModifier.AltModifier
PACKAGE = Path(__file__).resolve().parents[2] / "boswas_apps" / "calculator"


class CalculatorTestCase(testing.FluentTestCase):
    width, height = 360, 600

    def setUp(self) -> None:
        super().setUp()
        QLocale.setDefault(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.window = self.open()

    def open(self, arguments=None) -> CalculatorWindow:
        window = CalculatorWindow(arguments, state_directory=self.state_dir, today=date(2026, 11, 15))
        self.show(window)
        window.resize(self.width, self.height)        # after the show: a remembered size is restored there
        testing.process(10)
        return window

    # --- helpers ---
    @property
    def page(self):
        return self.window.calc_page

    def click_keys(self, text: str) -> None:
        """Click keys of the current keypad by their command; a number may be written in one piece."""
        for token in text.split():
            commands = list(token) if len(token) > 1 and all(c in "0123456789ABCDEF." for c in token) else [token]
            for command in commands:
                button = self.page._keys[self.page.mode].get(command)
                if button is None:
                    button = self.page._memory_buttons[self.page.mode].get(command)
                self.assertIsNotNone(button, f"no key {command!r} in {self.page.mode} mode")
                self.assertTrue(button.isEnabled(), f"the key {command!r} is disabled")
                testing.click(button)

    def type(self, text: str) -> None:
        for character in text:
            code = Qt.Key(ord(character.upper())) if character.isalnum() else Qt.Key.Key_unknown
            modifiers = SHIFT if character.isupper() else Qt.KeyboardModifier.NoModifier
            testing.key(QApplication.focusWidget() or self.window, code, modifiers, character)

    def press(self, key, modifiers=Qt.KeyboardModifier.NoModifier, text: str = "") -> None:
        testing.key(QApplication.focusWidget() or self.window, key, modifiers, text)

    def shortcut(self, keys: str) -> None:
        """Activate a window shortcut (the offscreen platform has no key routing for QShortcut)."""
        from PySide6.QtGui import QKeySequence, QShortcut
        for candidate in self.window.findChildren(QShortcut):
            if candidate.key() == QKeySequence(keys):
                candidate.activated.emit()
                testing.process()
                return
        self.fail(f"no shortcut {keys}")

    @property
    def display(self) -> str:
        return self.page.display.text()

    @property
    def expression(self) -> str:
        return self.page.expression_label.text()


class StandardTest(CalculatorTestCase):
    def test_starts_in_standard_mode(self):
        self.assertEqual(self.window.page, "standard")
        self.assertEqual(self.window.page_title(), "Standard")
        self.assertEqual(self.window.windowTitle(), "Standard - Calculator")
        self.assertEqual(self.display, "0")
        self.assertEqual(QGuiApplication.desktopFileName() or APP_ID, APP_ID)

    def test_clicking_keys(self):
        self.click_keys("2 + 3 * 4 =")
        self.assertEqual(self.display, "20")
        self.assertEqual(self.expression, "5 × 4 =")
        self.click_keys("c 50 + 10 percent =")
        self.assertEqual(self.display, "55")
        self.click_keys("c 9 sqrt reciprocal sqr sign")
        self.assertEqual(self.display, "-0.1111111111111111")
        self.click_keys("c 12.5 back back ce 7 = = =")
        self.assertEqual(self.display, "7")

    def test_typing_on_the_keyboard(self):
        self.type("12+3")
        self.press(Qt.Key.Key_Return)
        self.assertEqual(self.display, "15")
        self.type("*2=")
        self.assertEqual(self.display, "30")
        self.type("/4")
        self.press(Qt.Key.Key_Enter)
        self.assertEqual(self.display, "7.5")
        self.press(Qt.Key.Key_Escape)
        self.assertEqual(self.display, "0")
        self.type("1,5-0.5=")                      # the comma is a decimal key too
        self.assertEqual(self.display, "1")
        self.type("129")
        self.press(Qt.Key.Key_Backspace)
        self.assertEqual(self.display, "12")
        self.press(Qt.Key.Key_Delete)
        self.assertEqual(self.display, "0")
        self.type("9@")
        self.assertEqual(self.display, "3")
        self.type("r")
        self.assertEqual(self.display, "0.3333333333333333")
        self.press(Qt.Key.Key_Escape)
        self.type("5")
        self.press(Qt.Key.Key_F9)
        self.assertEqual(self.display, "-5")
        self.type("q")
        self.assertEqual(self.display, "25")
        self.type("+10%=")
        self.assertEqual(self.display, "27.5")

    def test_enter_is_equals_even_when_a_key_has_the_focus(self):
        self.click_keys("2 + 2")
        key = self.page._keys["standard"]["7"]
        key.setFocus()
        self.press(Qt.Key.Key_Return)
        self.assertEqual(self.display, "4")
        self.press(Qt.Key.Key_Space)               # Space presses the focused key
        self.assertEqual(self.display, "7")

    def test_error_locks_the_keys_until_c(self):
        self.click_keys("1 / 0 =")
        self.assertEqual(self.display, "Cannot divide by zero")
        keys = self.page._keys["standard"]
        self.assertFalse(any(keys[command].isEnabled() for command in ("5", "+", "=", "sqrt", "back", ".")))
        self.assertTrue(keys["c"].isEnabled() and keys["ce"].isEnabled())
        self.type("5+5=")
        self.assertEqual(self.display, "Cannot divide by zero")
        self.press(Qt.Key.Key_Escape)
        self.assertEqual(self.display, "0")
        self.assertTrue(keys["5"].isEnabled())
        self.type("5+5=")
        self.assertEqual(self.display, "10")

    def test_memory_keys_and_shortcuts(self):
        memory = self.page._memory_buttons["standard"]
        self.assertFalse(memory["mr"].isEnabled())
        self.assertFalse(memory["mc"].isEnabled())
        self.click_keys("5 ms 3 m+")
        self.assertTrue(memory["mr"].isEnabled())
        self.assertEqual(self.window.calculator.memory_text(0), "8")
        self.click_keys("c 2 * mr =")
        self.assertEqual(self.display, "16")
        self.press(Qt.Key.Key_Escape)
        self.type("4")
        self.press(Qt.Key.Key_Q, CTRL)             # M−
        self.assertEqual(self.window.calculator.memory_text(0), "4")
        self.press(Qt.Key.Key_P, CTRL)             # M+
        self.press(Qt.Key.Key_M, CTRL)             # MS
        self.assertEqual([self.window.calculator.memory_text(i) for i in range(2)], ["4", "8"])
        self.press(Qt.Key.Key_Escape)
        self.press(Qt.Key.Key_R, CTRL)             # MR
        self.assertEqual(self.display, "4")
        self.press(Qt.Key.Key_L, CTRL)             # MC
        self.assertEqual(self.window.calculator.memory, [])
        self.assertFalse(memory["mr"].isEnabled())

    def test_copy_and_paste(self):
        self.click_keys("1234.5 * 2 =")
        self.press(Qt.Key.Key_C, CTRL)
        self.assertEqual(QGuiApplication.clipboard().text(), "2,469")
        self.press(Qt.Key.Key_Escape)
        QGuiApplication.clipboard().setText(" 1,000.25 ")
        self.press(Qt.Key.Key_V, CTRL)
        self.assertEqual(self.display, "1,000.25")
        self.type("*2=")
        self.assertEqual(self.display, "2,000.5")
        QGuiApplication.clipboard().setText("not a number")
        self.press(Qt.Key.Key_V, CTRL)
        self.assertEqual(self.display, "Invalid input")
        self.press(Qt.Key.Key_Escape)
        self.assertEqual(self.display, "0")

    def test_history_sheet_in_a_narrow_window(self):
        self.click_keys("2 + 3 = 4 * 5 =")
        self.assertFalse(self.window.panel.isVisible())
        self.assertTrue(self.window.history_button.isVisible())
        testing.click(self.window.history_button)
        sheet = self.page.sheet
        self.assertTrue(sheet.isVisible())
        rows = sheet.history_list.rows
        self.assertEqual([row.texts() for row in rows], [("4 × 5 =", "20"), ("2 + 3 =", "5")])
        testing.click(rows[1])
        self.assertFalse(sheet.isVisible())
        self.assertEqual(self.display, "5")
        self.type("+1=")
        self.assertEqual(self.display, "6")
        self.shortcut("Ctrl+H")
        self.assertTrue(sheet.isVisible())
        testing.click(sheet.clear_button)
        self.assertEqual(self.window.calculator.history, [])
        self.assertTrue(sheet.history_list.empty_label.isVisible())
        self.press(Qt.Key.Key_Escape)
        self.assertFalse(sheet.isVisible())
        self.assertEqual(self.display, "6")        # Escape closed the sheet, it did not clear

    def test_memory_sheet(self):
        self.click_keys("5 ms 7 ms")
        more = self.page._memory_buttons["standard"]["memory"]
        self.assertTrue(more.isVisible() and more.isEnabled())
        testing.click(more)
        sheet = self.page.sheet
        self.assertTrue(sheet.isVisible())
        self.assertEqual(sheet.current_tab(), MEMORY)
        rows = sheet.memory_list.rows
        self.assertEqual([row.value_row.texts()[1] for row in rows], ["7", "5"])
        testing.click(rows[1].add_button)          # 5 + the display (7)
        self.assertEqual(self.window.calculator.memory_text(1), "12")
        testing.click(sheet.memory_list.rows[0].subtract_button)
        self.assertEqual(self.window.calculator.memory_text(0), "0")
        testing.click(sheet.memory_list.rows[0].clear_button)
        self.assertEqual(len(self.window.calculator.memory), 1)
        testing.click(sheet.memory_list.rows[0].value_row)
        self.assertEqual(self.display, "12")
        self.assertFalse(sheet.isVisible())

    def test_clear_history_shortcut(self):
        self.click_keys("1 + 1 =")
        self.shortcut("Ctrl+Shift+D")
        self.assertEqual(self.window.calculator.history, [])

    def test_the_display_shrinks_its_text_to_fit(self):
        self.assertEqual(self.page.display.fitted_style(), "title-large")
        self.click_keys("1 / 3 =")
        self.assertEqual(self.display, "0.3333333333333333")
        self.assertNotEqual(self.page.display.fitted_style(), "title-large")
        self.window.resize(900, 600)
        testing.process(10)
        self.assertEqual(self.page.display.fitted_style(), "title-large")

    def test_display_context_menu_is_built(self):
        from boswas_apps.calculator.ui.widgets import copy_paste_menu
        menu = copy_paste_menu(self.page, self.page.copy, self.page.paste, can_paste=False)
        self.assertEqual([action.text() for action in menu.actions()], ["Copy", "Paste"])
        self.assertFalse(menu.actions()[1].isEnabled())
        self.click_keys("42")
        menu.actions()[0].trigger()
        self.assertEqual(QGuiApplication.clipboard().text(), "42")


class WideWindowTest(CalculatorTestCase):
    width, height = 800, 600

    def test_history_and_memory_stand_beside_the_keypad(self):
        self.assertTrue(self.window.panel.isVisible())
        self.assertFalse(self.window.history_button.isVisible())
        self.assertFalse(self.page._memory_buttons["standard"]["memory"].isVisible())
        self.click_keys("6 * 7 = ms")
        panel = self.window.panel
        self.assertEqual([row.texts() for row in panel.history_list.rows], [("6 × 7 =", "42")])
        self.assertEqual(len(panel.memory_list.rows), 1)
        self.click_keys("c")
        testing.click(panel.history_list.rows[0])
        self.assertEqual(self.display, "42")
        testing.click(panel.pivot.item(MEMORY))
        self.assertEqual(panel.current_tab(), MEMORY)
        testing.click(panel.clear_button)
        self.assertEqual(self.window.calculator.memory, [])
        self.shortcut("Ctrl+H")
        self.assertEqual(panel.current_tab(), HISTORY)

    def test_narrowing_the_window_hides_the_panel(self):
        self.window.resize(360, 600)
        testing.process(10)
        self.assertFalse(self.window.panel.isVisible())
        self.window.resize(800, 600)
        testing.process(10)
        self.assertTrue(self.window.panel.isVisible())


class NavigationTest(CalculatorTestCase):
    def test_hamburger_menu(self):
        navigation = self.window.navigation
        self.assertFalse(navigation.is_open())
        testing.click(self.window.menu_button)
        self.assertTrue(navigation.is_open())
        self.assertEqual(navigation.keys(), ["standard", "scientific", "programmer", "date", "length", "weight",
                                             "temperature", "area", "volume", "speed", "time", "data", "energy",
                                             "power", "pressure", "angle"])
        self.assertNotIn("currency", navigation.keys())
        navigation.click_item("scientific")
        self.assertFalse(navigation.is_open())
        self.assertEqual(self.window.page_title(), "Scientific")
        self.assertIs(self.window.stack.currentWidget(), self.page)

    def test_keyboard_in_the_menu(self):
        testing.click(self.window.menu_button)
        navigation = self.window.navigation
        testing.key(navigation.list, Qt.Key.Key_Down)
        testing.key(navigation.list, Qt.Key.Key_Down)
        testing.key(navigation.list, Qt.Key.Key_Down)
        testing.key(navigation.list, Qt.Key.Key_Down)          # over the "Converter" title to Length
        testing.key(navigation.list, Qt.Key.Key_Return)
        self.assertEqual(self.window.page, "length")
        testing.click(self.window.menu_button)
        testing.key(navigation, Qt.Key.Key_Escape)
        self.assertFalse(navigation.is_open())

    def test_the_menu_slides_in_at_the_refresh_rate_of_the_screen(self):
        from unittest import mock

        from PySide6.QtGui import QScreen
        navigation = self.window.navigation
        with mock.patch.object(QScreen, "refreshRate", lambda _screen: 144.0):
            self.window.open_navigation()
            slide = navigation._animation
            # The toolkit's animation, stepping once per refresh of the window's screen (Qt's own would step at 60 Hz).
            self.assertIsInstance(slide, fluent.Animation)
            self.assertEqual(slide.refresh_rate(), 144.0)
            self.assertTrue(testing.wait_until(lambda: slide.state() != fluent.Animation.State.Running, 2))
        self.assertEqual(navigation.pane.x(), 0)

    def test_click_beside_the_menu_closes_it(self):
        self.window.open_navigation()
        testing.click(self.window.navigation, QPoint(340, 300))
        self.assertFalse(self.window.navigation.is_open())
        self.assertEqual(self.window.page, "standard")

    def test_typing_does_not_reach_the_keypad_while_the_menu_is_open(self):
        self.window.open_navigation()
        testing.key(self.window.navigation, Qt.Key.Key_5, text="5")
        self.assertEqual(self.display, "0")

    def test_alt_number_switches_the_mode(self):
        for keys, title in (("Alt+2", "Scientific"), ("Alt+3", "Programmer"), ("Alt+4", "Date calculation"),
                            ("Alt+1", "Standard")):
            self.shortcut(keys)
            self.assertEqual(self.window.page_title(), title)

    def test_switching_the_mode_starts_a_new_calculation(self):
        self.click_keys("5 + 3")
        self.window.set_page("scientific")
        self.assertEqual(self.display, "0")
        self.window.set_page("standard")
        self.assertEqual(self.display, "0")
        with self.assertRaises(ValueError):
            self.window.set_page("graphing")


class ScientificTest(CalculatorTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.window.set_page("scientific")

    def test_precedence_and_parentheses(self):
        self.click_keys("2 + 3 * 4 =")
        self.assertEqual(self.display, "14")
        self.click_keys("c 2 * ( 3 + 4")
        self.assertEqual(self.page._open_keys["scientific"].text(), "(₁")
        self.click_keys(") =")
        self.assertEqual(self.display, "14")
        self.assertEqual(self.page._open_keys["scientific"].text(), "(")
        self.type("(1+2)*3=")
        self.assertEqual(self.display, "9")

    def test_second_functions(self):
        second = self.page._second_keys
        self.assertEqual(second["sqr"].command, "sqr")
        testing.click(self.page.second_button)
        self.assertEqual([second[name].command for name in ("sqr", "sqrt", "pow", "pow10", "log", "ln")],
                         ["cube", "cuberoot", "root", "pow2", "logbasey", "powe"])
        self.click_keys("3")
        testing.click(second["sqr"])
        self.assertEqual(self.display, "27")
        self.assertEqual(second["sqr"].command, "sqr")          # 2nd applies to one key
        testing.click(second["sqrt"])
        self.assertEqual(self.display, "5.1961524227066318805823390245176")

    def test_scientific_keys(self):
        self.click_keys("5 fact")
        self.assertEqual(self.display, "120")
        self.click_keys("c pi")
        self.assertEqual(self.display, "3.1415926535897932384626433832795")
        self.click_keys("c 1.5 exp 3")
        self.assertEqual(self.display, "1.5e+3")
        self.click_keys("= c 7 mod 4 =")
        self.assertEqual(self.display, "3")
        self.click_keys("c 2")
        testing.click(self.page._second_keys["pow"])
        self.click_keys("10 =")
        self.assertEqual(self.display, "1,024")
        self.click_keys("c 100")
        testing.click(self.page._second_keys["log"])
        self.assertEqual(self.display, "2")
        self.click_keys("c 5 sign abs")
        self.assertEqual(self.display, "5")

    def test_angle_unit_button_and_keys(self):
        self.assertEqual(self.page.angle_button.text(), "DEG")
        self.click_keys("90")
        self.type("s")
        self.assertEqual(self.display, "1")
        testing.click(self.page.angle_button)
        self.assertEqual(self.page.angle_button.text(), "RAD")
        self.click_keys("c pi")
        self.type("o")
        self.assertEqual(self.display, "-1")
        testing.click(self.page.angle_button)
        self.assertEqual(self.page.angle_button.text(), "GRAD")
        self.click_keys("c 100")
        self.type("s")
        self.assertEqual(self.display, "1")
        self.press(Qt.Key.Key_F3)
        self.assertEqual(self.page.angle_button.text(), "DEG")
        self.press(Qt.Key.Key_F4)
        self.assertEqual(self.page.angle_button.text(), "RAD")
        self.press(Qt.Key.Key_F5)
        self.assertEqual(self.page.angle_button.text(), "GRAD")
        self.assertEqual(self.window.state["angle"], "grad")

    def test_trigonometry_flyout(self):
        self.click_keys("30")
        testing.click(self.page.trig_button)
        flyout = self.page._flyout
        self.assertTrue(flyout is not None and flyout.isVisible())
        testing.click(self.page.trig_keys["sin"])
        self.assertEqual(self.display, "0.5")
        self.assertIsNone(self.page._flyout)
        testing.click(self.page.trig_button)
        testing.click(self.page.trig_second_key)
        self.assertEqual(self.page.trig_keys["sin"].text(), "sin⁻¹")
        testing.click(self.page.trig_keys["sin"])
        self.assertEqual(self.display, "30")
        self.click_keys("c 0")
        testing.click(self.page.trig_button)
        self.assertTrue(self.page.trig_second_key.isChecked())   # the switches stay as they were left
        testing.click(self.page.trig_second_key)
        testing.click(self.page.trig_hyp_key)
        self.assertEqual(self.page.trig_keys["cos"].text(), "cosh")
        testing.click(self.page.trig_keys["cos"])
        self.assertEqual(self.display, "1")

    def test_function_flyout(self):
        self.click_keys("2.5")
        testing.click(self.page.function_button)
        testing.click(self.page.function_keys["floor"])
        self.assertEqual(self.display, "2")
        self.click_keys("c 2.5")
        testing.click(self.page.function_button)
        testing.click(self.page.function_keys["dms"])
        self.assertEqual(self.display, "2.3")
        testing.click(self.page.function_button)
        testing.click(self.page.function_keys["rand"])
        self.assertTrue(0 <= self.window.calculator.engine.value < 1)

    def test_fe_key(self):
        self.click_keys("1234.5 =")
        testing.click(self.page.fe_button)
        self.assertEqual(self.display, "1.2345e+3")
        self.assertTrue(self.page.fe_button.isChecked())
        self.type("v")
        self.assertEqual(self.display, "1,234.5")

    def test_keyboard_functions(self):
        for typed, expected in (("9@", "3"), ("5!", "120"), ("2^10=", "1,024"), ("100l", "2"), ("3#", "27"),
                                ("4q", "16"), ("8r", "0.125"), ("7%4=", "3"), ("2x3=", "2,000")):
            with self.subTest(typed=typed):
                self.press(Qt.Key.Key_Escape)
                self.type(typed)
                self.assertEqual(self.display, expected)
        self.press(Qt.Key.Key_Escape)
        self.type("e")
        self.press(Qt.Key.Key_N)
        self.type("n")
        self.assertEqual(self.display, "1")
        self.press(Qt.Key.Key_Escape)
        self.type("1")
        self.press(Qt.Key.Key_N, CTRL)             # e to the x
        self.assertEqual(self.display, "2.7182818284590452353602874713527")
        self.press(Qt.Key.Key_Escape)
        self.type("3")
        self.press(Qt.Key.Key_G, CTRL)             # ten to the x
        self.assertEqual(self.display, "1,000")
        self.press(Qt.Key.Key_Escape)
        self.type("27")
        self.press(Qt.Key.Key_Y, CTRL)             # y-th root
        self.type("3=")
        self.assertEqual(self.display, "3")
        self.press(Qt.Key.Key_Escape)
        self.type("0")
        self.press(Qt.Key.Key_S, CTRL)             # sinh
        self.assertEqual(self.display, "0")
        self.press(Qt.Key.Key_Escape)
        self.type("1")
        self.press(Qt.Key.Key_S, SHIFT, "S")       # inverse sine, in degrees
        self.assertEqual(self.display, "90")

    def test_32_digits_fit_the_narrow_window(self):
        self.click_keys("1 / 3 =")
        self.assertEqual(self.display, "0." + "3" * 32)
        display = self.page.display
        metrics = fluent.font(display.fitted_style())
        from PySide6.QtGui import QFontMetrics
        self.assertLessEqual(QFontMetrics(metrics).horizontalAdvance(display.text()), display.width())

    def test_ce_and_c_share_a_key(self):
        clear = self.page._clear_keys["scientific"]
        self.assertEqual(clear.text(), "C")
        self.click_keys("5 + 3")
        self.assertEqual(clear.text(), "CE")
        testing.click(clear)
        self.assertEqual((self.display, clear.text()), ("0", "C"))
        self.click_keys("4 =")
        self.assertEqual(self.display, "9")


class ProgrammerTest(CalculatorTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.window.set_page("programmer")

    def test_four_radixes_at_once(self):
        self.click_keys("255")
        rows = self.page.radix_rows
        self.assertEqual([rows[radix].value() for radix in (16, 10, 8, 2)], ["FF", "255", "377", "1111 1111"])
        self.assertTrue(rows[10].isChecked())
        testing.click(rows[16])
        self.assertTrue(rows[16].isChecked() and not rows[10].isChecked())
        self.assertEqual(self.display, "FF")
        self.click_keys("+ 1 =")
        self.assertEqual(self.display, "100")
        self.assertEqual(rows[10].value(), "256")

    def test_letter_keys_follow_the_radix(self):
        keys = self.page._keys["programmer"]
        self.assertFalse(any(keys[letter].isEnabled() for letter in "ABCDEF"))
        self.assertFalse(keys["."].isEnabled())
        testing.click(self.page.radix_rows[16])
        self.assertTrue(all(keys[letter].isEnabled() for letter in "ABCDEF"))
        testing.click(self.page.radix_rows[8])
        self.assertFalse(keys["8"].isEnabled() or keys["9"].isEnabled())
        self.assertTrue(keys["7"].isEnabled())
        testing.click(self.page.radix_rows[2])
        self.assertFalse(keys["2"].isEnabled())
        self.assertTrue(keys["1"].isEnabled())

    def test_word_size_button(self):
        self.click_keys("1 sign")
        self.assertEqual(self.page.word_button.text(), "QWORD")
        self.assertEqual(self.page.radix_rows[16].value(), "FFFF FFFF FFFF FFFF")
        testing.click(self.page.word_button)
        self.assertEqual(self.page.word_button.text(), "DWORD")
        self.assertEqual(self.page.radix_rows[16].value(), "FFFF FFFF")
        testing.click(self.page.word_button)
        testing.click(self.page.word_button)
        self.assertEqual(self.page.word_button.text(), "BYTE")
        self.assertEqual(self.display, "-1")
        self.click_keys("c 127 + 1 =")
        self.assertEqual(self.display, "-128")
        testing.click(self.page.word_button)
        self.assertEqual(self.page.word_button.text(), "QWORD")
        self.assertEqual(self.display, "-128")
        self.assertEqual(self.window.state["bits"], 64)

    def test_bitwise_flyout_and_keys(self):
        self.click_keys("6")
        testing.click(self.page.bitwise_button)
        testing.click(self.page.bitwise_keys["and"])
        self.click_keys("3 =")
        self.assertEqual(self.display, "2")
        testing.click(self.page.bitwise_button)
        testing.click(self.page.bitwise_keys["not"])
        self.assertEqual(self.display, "-3")
        self.press(Qt.Key.Key_Escape)
        self.type("6|3=")
        self.assertEqual(self.display, "7")
        self.type("^5=")
        self.assertEqual(self.display, "2")
        self.type("~")
        self.assertEqual(self.display, "-3")
        self.press(Qt.Key.Key_Escape)
        self.type("7%3=")
        self.assertEqual(self.display, "1")

    def test_shift_modes(self):
        self.click_keys("1")
        testing.click(self.page.left_shift_key)
        self.click_keys("4 =")
        self.assertEqual(self.display, "16")
        menu = self.page._shift_menu()
        self.assertEqual([action.text() for action in menu.actions()],
                         ["Arithmetic shift", "Logical shift", "Rotate circular shift",
                          "Rotate through carry circular shift"])
        self.assertTrue(menu.actions()[0].isChecked())
        self.click_keys("c 1 sign")
        testing.click(self.page.right_shift_key)
        self.click_keys("1 =")
        self.assertEqual(self.display, "-1")                    # arithmetic: the sign stays
        menu.actions()[1].trigger()                             # logical
        self.click_keys("c 1 sign")
        testing.click(self.page.right_shift_key)
        self.click_keys("1 =")
        self.assertEqual(self.display, "9,223,372,036,854,775,807")
        self.page._shift_menu().actions()[2].trigger()          # rotate
        self.assertEqual(self.page.left_shift_key.text(), "RoL")
        testing.click(self.page.word_button)
        testing.click(self.page.word_button)
        testing.click(self.page.word_button)                    # BYTE
        self.click_keys("c 64")
        testing.click(self.page.left_shift_key)
        self.assertEqual(self.display, "-128")
        testing.click(self.page.left_shift_key)
        self.assertEqual(self.display, "1")
        self.page._shift_menu().actions()[3].trigger()          # rotate through carry
        self.click_keys("c 1")
        testing.click(self.page.right_shift_key)
        self.assertEqual(self.display, "0")
        testing.click(self.page.right_shift_key)
        self.assertEqual(self.display, "-128")
        self.assertEqual(self.window.state["shift"], "rotate_carry")

    def test_bit_keypad(self):
        testing.click(self.page.bit_pad_button)
        self.assertIs(self.page.programmer_pads.currentWidget(), self.page.bit_pad)
        bits = self.page.bit_pad.buttons
        testing.click(bits[3])
        testing.click(bits[0])
        self.assertEqual(self.display, "9")
        self.assertTrue(bits[3].isChecked() and bits[0].isChecked() and not bits[1].isChecked())
        testing.click(bits[3])
        self.assertEqual(self.display, "1")
        testing.click(self.page.word_button)                    # DWORD: the upper bits are off
        self.assertFalse(bits[32].isEnabled())
        self.assertTrue(bits[31].isEnabled())
        testing.click(bits[31])
        self.assertEqual(self.display, "-2,147,483,647")
        testing.click(self.page.full_pad_button)
        self.assertIsNot(self.page.programmer_pads.currentWidget(), self.page.bit_pad)

    def test_function_keys_choose_radix_and_word_size(self):
        self.click_keys("255")
        self.press(Qt.Key.Key_F5)
        self.assertEqual(self.display, "FF")
        self.type("a")
        self.assertEqual(self.display, "A")
        self.press(Qt.Key.Key_F8)
        self.assertEqual(self.display, "1010")
        self.press(Qt.Key.Key_F7)
        self.assertEqual(self.display, "12")
        self.press(Qt.Key.Key_F6)
        self.assertEqual(self.display, "10")
        for key, label in ((Qt.Key.Key_F2, "DWORD"), (Qt.Key.Key_F3, "WORD"), (Qt.Key.Key_F4, "BYTE"),
                           (Qt.Key.Key_F12, "QWORD")):
            self.press(key)
            self.assertEqual(self.page.word_button.text(), label)

    def test_programmer_has_memory_but_no_history(self):
        self.assertFalse(self.window.history_button.isVisible())
        self.click_keys("5 + 3 = ms")
        self.assertEqual(self.window.calculator.history, [])
        testing.click(self.page._memory_buttons["programmer"]["memory"])
        sheet = self.page.sheet
        self.assertTrue(sheet.isVisible())
        self.assertEqual(sheet.current_tab(), MEMORY)
        self.assertTrue(sheet.pivot.item(HISTORY).isHidden())
        self.assertEqual(sheet.memory_list.rows[0].value_row.texts()[1], "8")

    def test_paste_is_validated_against_the_radix(self):
        QGuiApplication.clipboard().setText("12")
        self.press(Qt.Key.Key_V, CTRL)
        self.assertEqual(self.display, "12")
        QGuiApplication.clipboard().setText("1.5")
        self.press(Qt.Key.Key_V, CTRL)
        self.assertEqual(self.display, "Invalid input")


class DateTest(CalculatorTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.window.set_page("date")
        self.dates = self.window.date_page

    def test_difference(self):
        self.assertIs(self.window.stack.currentWidget(), self.dates)
        self.assertEqual(self.dates.from_edit.date(), QDate(2026, 11, 15))
        self.assertEqual(self.dates.difference_label.text(), "Same dates")
        self.dates.to_edit.setDate(QDate(2028, 2, 29))
        self.assertEqual(self.dates.difference_label.text(), "1 year, 3 months, 2 weeks")
        self.assertEqual(self.dates.total_label.text(), "471 days")
        self.dates.from_edit.setDate(QDate(2028, 3, 1))
        self.assertEqual(self.dates.difference_label.text(), "1 day")
        self.assertFalse(self.dates.total_label.isVisible())

    def test_add_and_subtract(self):
        self.dates.mode_box.setCurrentIndex(1)
        self.assertTrue(self.dates.shift_box.isVisible())
        self.assertFalse(self.dates.difference_box.isVisible())
        self.dates.from_edit.setDate(QDate(2024, 1, 31))
        self.dates.span_boxes["months"].setValue(1)
        self.assertEqual(self.dates.result_date, date(2024, 2, 29))
        self.assertEqual(self.dates.result_label.text(), "Thursday, February 29, 2024")
        self.dates.span_boxes["years"].setValue(1)
        self.dates.span_boxes["days"].setValue(1)
        self.assertEqual(self.dates.result_date, date(2025, 3, 1))
        testing.click(self.dates.subtract_radio)
        self.assertEqual(self.dates.result_date, date(2022, 12, 30))
        self.dates.from_edit.setDate(QDate(1, 1, 5))
        self.assertEqual(self.dates.result_label.text(), "Date out of bounds")
        self.assertEqual(self.window.state["date_mode"], "shift")

    def test_digits_typed_here_do_not_reach_a_calculator(self):
        self.type("5")
        self.assertEqual(self.window.calculator.engine.display, "0")


class ConverterTest(CalculatorTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.window.set_page("length")
        self.converter = self.window.converter_page

    def click(self, text: str) -> None:
        for token in text.split():
            for command in (list(token) if token[0].isdigit() and len(token) > 1 else [token]):
                testing.click(self.converter.keys[command])

    def test_live_conversion(self):
        self.assertEqual(self.window.page_title(), "Length")
        self.assertEqual([box.currentData() for box in self.converter.unit_boxes], ["centimeter", "inch"])
        self.click("2 . 5 4")
        self.assertEqual((self.converter.text(0), self.converter.text(1)), ("2.54", "1"))
        self.click("back back")
        self.assertEqual((self.converter.text(0), self.converter.text(1)), ("2.", "0.7874016"))
        self.click("ce 100")
        self.assertEqual(self.converter.text(1), "39.37008")

    def test_both_ways(self):
        self.click("100")
        self.converter.values[1].setFocus()        # as a click or Tab does
        self.assertEqual(self.converter.active, 1)
        self.assertEqual(self.converter.text(1), "39.3700787401575")
        self.click("ce 12")
        self.assertEqual((self.converter.text(0), self.converter.text(1)), ("30.48", "12"))
        self.assertTrue(self.converter.values[1].is_active())
        self.assertFalse(self.converter.values[0].is_active())

    def test_changing_a_unit_converts_again_and_is_remembered(self):
        self.click("1")
        self.converter.set_unit(0, "mile")
        self.converter.set_unit(1, "kilometer")
        self.assertEqual(self.converter.text(1), "1.609344")
        self.assertEqual(self.window.state["units"]["length"], ["mile", "kilometer"])
        self.window.set_page("weight")
        self.assertEqual(self.converter.text(0), "0")
        self.window.set_page("length")
        self.assertEqual([box.currentData() for box in self.converter.unit_boxes], ["mile", "kilometer"])

    def test_keyboard(self):
        self.type("12.5")
        self.assertEqual(self.converter.text(0), "12.5")
        self.press(Qt.Key.Key_Backspace)
        self.assertEqual(self.converter.text(0), "12.")
        self.press(Qt.Key.Key_Escape)
        self.assertEqual(self.converter.text(0), "0")
        self.type("7,5")
        self.assertEqual(self.converter.text(0), "7.5")

    def test_sign_only_where_it_makes_sense(self):
        self.assertFalse(self.converter.keys["sign"].isEnabled())
        self.window.set_page("temperature")
        self.assertTrue(self.converter.keys["sign"].isEnabled())
        self.click("4 0 sign")
        self.assertEqual((self.converter.text(0), self.converter.text(1)), ("-40", "-40"))
        self.press(Qt.Key.Key_F9)
        self.assertEqual((self.converter.text(0), self.converter.text(1)), ("40", "104"))

    def test_every_category_opens(self):
        from boswas_apps.calculator import units
        for category in units.CATEGORIES:
            with self.subTest(category=category.key):
                self.window.set_page(category.key)
                self.assertEqual(self.window.page_title(), category.name)
                self.assertEqual(self.converter.unit_boxes[0].count(), len(category.units))
                self.click("1")
                self.assertNotEqual(self.converter.text(1), "")

    def test_copy_and_paste(self):
        self.click("2 . 5 4")
        self.press(Qt.Key.Key_C, CTRL)
        self.assertEqual(QGuiApplication.clipboard().text(), "2.54")
        self.converter.copy(1)
        self.assertEqual(QGuiApplication.clipboard().text(), "1")
        QGuiApplication.clipboard().setText("1,000")
        self.press(Qt.Key.Key_V, CTRL)
        self.assertEqual(self.converter.text(0), "1,000")
        for refused in ("abc", "-5", "1e30", "12345678901234567890"):
            QGuiApplication.clipboard().setText(refused)
            self.assertFalse(self.converter.paste())
            self.assertEqual(self.converter.text(0), "1,000")


class StateTest(CalculatorTestCase):
    def test_the_last_mode_and_choices_come_back(self):
        self.window.set_page("scientific")
        testing.click(self.page.angle_button)
        self.window.set_page("programmer")
        testing.click(self.page.radix_rows[16])
        testing.click(self.page.word_button)
        self.window.set_page("temperature")
        self.window.converter_page.set_unit(1, "kelvin")
        self.window.close()
        saved = json.loads((self.state_dir / state.FILE_NAME).read_text())
        self.assertEqual((saved["page"], saved["angle"], saved["radix"], saved["bits"]),
                         ("temperature", "rad", 16, 32))
        again = self.open()
        self.assertEqual(again.page_title(), "Temperature")
        self.assertEqual(again.converter_page.unit_boxes[1].currentData(), "kelvin")
        again.set_page("programmer")
        self.assertEqual(again.calc_page.word_button.text(), "DWORD")
        self.assertTrue(again.calc_page.radix_rows[16].isChecked())
        again.set_page("scientific")
        self.assertEqual(again.calc_page.angle_button.text(), "RAD")

    def test_a_mode_can_be_asked_for(self):
        window = self.open(["programmer"])
        self.assertEqual(window.page_title(), "Programmer")
        window = self.open(["nonsense"])
        self.assertEqual(window.page_title(), "Standard")

    def test_a_damaged_state_file_gives_the_defaults(self):
        for content in (b"", b"{", b"[1, 2]", b'{"page": "currency", "bits": 7, "radix": true, "units": 5}',
                        b"\xff\xfe\x00", b'{"units": {"length": ["meter", "parsec"], "weight": "x"}}',
                        b"[" * 100000, b"x" * (state.MAX_BYTES + 10)):
            with self.subTest(content=content[:30]):
                (self.state_dir / state.FILE_NAME).write_bytes(content)
                self.assertEqual(state.load(self.state_dir), state.defaults())
        (self.state_dir / state.FILE_NAME).write_text(json.dumps({"page": "angle", "bits": 16, "evil": "x" * 100}))
        loaded = state.load(self.state_dir)
        self.assertEqual((loaded["page"], loaded["bits"]), ("angle", 16))
        self.assertNotIn("evil", loaded)
        self.assertEqual(state.load(self.tmp / "missing"), state.defaults())
        self.assertFalse(state.save(self.tmp / "missing", state.defaults()))


class LookTest(CalculatorTestCase):
    def test_every_key_has_a_name_and_the_icon_buttons_a_tool_tip(self):
        from boswas_apps.calculator.ui.widgets import KeyButton
        for mode in ("standard", "scientific", "programmer"):
            self.window.set_page(mode)
            for button in self.page._pad_widgets[mode].findChildren(KeyButton):
                self.assertTrue(button.accessibleName(), f"a key without a name in {mode} mode")
        self.assertTrue(self.window.menu_button.toolTip())
        self.assertTrue(self.window.history_button.toolTip())

    def test_light_and_dark_render(self):
        self.click_keys("1 / 3 =")
        for scheme in ("dark", "light"):
            testing.set_scheme(scheme)
            image = self.window.grab().toImage()
            self.assertEqual((image.width(), image.height()), (360, 600))
            background = fluent.qcolor("background")
            self.assertEqual(image.pixelColor(2, 598).name(), background.name())

    def test_no_colour_values_in_the_sources(self):
        pattern = re.compile(r"#[0-9A-Fa-f]{6}\b|#[0-9A-Fa-f]{3}\b|rgba?\(|QColor\(\s*\d|setStyleSheet")
        for path in sorted(PACKAGE.rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                self.assertIsNone(pattern.search(line), f"{path.name}:{number}: {line.strip()}")

    def test_scales_down_to_the_minimum_size(self):
        self.window.set_page("scientific")
        self.window.resize(self.window.minimumSize())
        testing.process(10)
        for button in self.page._keys["scientific"].values():
            self.assertGreaterEqual(button.height(), 28)
            self.assertGreaterEqual(button.width(), 32)
        self.assertLessEqual(self.page.pads.geometry().bottom(), self.page.height())


class NewWindowTest(unittest.TestCase):
    def test_every_start_is_a_calculator_of_its_own_and_new_window_says_so(self):
        from unittest import mock

        from boswas_apps.calculator import __main__ as cli
        options, rest = cli.parse_arguments(["--new-window", "--mode", "scientific"])
        self.assertTrue(options.new_window)
        self.assertEqual((options.mode, rest), ("scientific", []))
        seen = {}
        with mock.patch.object(fluent, "run", lambda app_id, factory, **kw: seen.update(kw) or 0):
            self.assertEqual(cli.main(["boswas-calculator", "--new-window"]), 0)
        self.assertFalse(seen["single_instance"], "no running calculator to hand the start to")
        entry = (PACKAGE.parents[1] / "data" / "calculator" / "com.boswas.Calculator.desktop").read_text()
        self.assertIn("Actions=NewWindow;Scientific;Programmer;\n", entry)
        self.assertIn("[Desktop Action NewWindow]\nName=New Window\nExec=boswas-calculator --new-window\n", entry)


if __name__ == "__main__":
    unittest.main()
