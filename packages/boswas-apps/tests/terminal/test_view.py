"""The terminal widget: drawing, keyboard, mouse, selection, clipboard, scrolling."""

import base64
import os
import subprocess
import sys
import time
import unittest

from term_fixtures import ALT, CTRL, SHIFT, cell_point, drag, mouse, testing, wheel

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QGuiApplication, QInputMethodEvent

import boswas_fluent as fluent

from boswas_apps.terminal import schemes
from boswas_apps.terminal import settings as settings_module
from boswas_apps.terminal.view import TerminalView


class ViewCase(testing.FluentTestCase):
    def view(self, width=640, height=360, text=""):
        view = TerminalView()
        self.sent = []
        view.send.connect(self.sent.append)
        self.show(view, width, height)
        view.setFocus()
        if text:
            view.feed(text.encode())
        view.flush()
        testing.process()
        return view

    def feed(self, view, text):
        view.feed(text.encode() if isinstance(text, str) else text)
        view.flush()

    def pixel(self, view, column, row, dx=0.5, dy=0.5):
        cell_w, cell_h = view.cell_size()
        return view._image.pixelColor(int(view._pad_x + (column + dx) * cell_w), int(view._pad_y + (row + dy) * cell_h))

    def has_ink(self, view, column, row):
        cell_w, cell_h = view.cell_size()
        background = view.background().rgb()
        x0, y0 = view._pad_x + column * cell_w, view._pad_y + row * cell_h
        return any(view._image.pixel(x, y) != background for x in range(x0, x0 + cell_w) for y in range(y0, y0 + cell_h))


class Drawing(ViewCase):
    def test_output_is_drawn_at_the_refresh_rate_of_the_screen(self):
        view = self.view()
        from unittest import mock
        from boswas_apps.terminal import view as view_module
        for rate, frame in ((60.0, 16), (144.0, 6), (240.0, 4)):
            with mock.patch.object(view_module.fluent, "refresh_rate", return_value=rate):
                self.assertEqual(view.frame_ms(), frame, rate)

    def test_grid_fits_the_widget(self):
        view = self.view(640, 360)
        cols, rows = view.grid_size()
        cell_w, cell_h = view.cell_size()
        self.assertGreater(cols, 40)
        self.assertGreater(rows, 10)
        self.assertLessEqual(cols * cell_w + 2 * view._pad_x, 640)
        self.assertLessEqual(rows * cell_h + 2 * view._pad_y, 360)
        self.assertGreater((cols + 1) * cell_w + 2 * view._pad_x + view.scrollbar().sizeHint().width(), 640)

    def test_text_is_drawn_where_it_belongs(self):
        view = self.view(text="A  B")
        self.assertTrue(self.has_ink(view, 0, 0))
        self.assertFalse(self.has_ink(view, 1, 0))
        self.assertTrue(self.has_ink(view, 3, 0))
        self.assertFalse(self.has_ink(view, 0, 2))

    def test_colours_come_from_the_scheme(self):
        view = self.view(text="\x1b[41m \x1b[48;2;1;2;3m \x1b[7m \x1b[0m\x1b[48;5;21m ")
        scheme = view.scheme()
        self.assertEqual(self.pixel(view, 0, 0), QColor(scheme.colors[1]))
        self.assertEqual(self.pixel(view, 1, 0), QColor(1, 2, 3))
        self.assertEqual(self.pixel(view, 3, 0), QColor(0, 0, 255))
        self.assertEqual(self.pixel(view, 10, 3), QColor(scheme.background))
        self.assertEqual(view.background(), QColor(scheme.background))

    def test_only_changed_rows_are_drawn_again(self):
        view = self.view(text="one\r\ntwo\r\nthree")
        painted = []
        original = view._paint_rows
        view._paint_rows = lambda rows, cursor: (painted.append(list(rows)), original(rows, cursor))
        self.feed(view, "\x1b[2;1Hchanged")
        self.assertEqual(painted, [[1, 2]])              # the row that changed and the one the cursor left
        painted.clear()
        view.flush()
        self.assertEqual(painted, [])

    def test_output_is_drawn_at_most_about_sixty_times_a_second(self):
        view = self.view()
        frames = []
        original = view._paint_rows
        view._paint_rows = lambda rows, cursor: (frames.append(time.monotonic()), original(rows, cursor))
        started = time.monotonic()
        while time.monotonic() - started < 0.3:
            view.feed(b"x" * 50 + b"\r\n")
            testing.process()
        elapsed = time.monotonic() - started
        self.assertGreater(len(frames), 3)
        self.assertLessEqual(len(frames), elapsed / 0.016 + 2)

    def test_wide_and_combining_characters_keep_the_grid(self):
        view = self.view(text="日本|é|क्षि|")
        self.assertTrue(self.has_ink(view, 0, 0) and self.has_ink(view, 2, 0))
        for column in (4, 6, 10):
            self.assertEqual(view.term.lines[0].chars[column], "|")
            x = view._pad_x + column * view.cell_size()[0] + view.cell_size()[0] // 2
            column_ink = [view._image.pixel(x, view._pad_y + y) != view.background().rgb()
                          for y in range(view.cell_size()[1])]
            self.assertGreater(sum(column_ink), view.cell_size()[1] // 2, column)

    def test_box_drawing_lines_meet(self):
        view = self.view(text="┌──┐\r\n│  │\r\n└──┘")
        cell_w, cell_h = view.cell_size()
        y = view._pad_y + cell_h // 2
        row = [view._image.pixel(x, y) != view.background().rgb()
               for x in range(view._pad_x + cell_w // 2, view._pad_x + 3 * cell_w + cell_w // 2)]
        self.assertTrue(all(row), "the horizontal line has a gap")
        x = view._pad_x + cell_w // 2
        column = [view._image.pixel(x, yy) != view.background().rgb()
                  for yy in range(view._pad_y + cell_h // 2, view._pad_y + 2 * cell_h + cell_h // 2)]
        self.assertTrue(all(column), "the vertical line has a gap")

    def test_block_cursor_inverts_the_cell(self):
        view = self.view()
        profile = settings_module.Profile("p", "P", cursor_shape="block", cursor_blink=False)
        view.configure(profile, settings_module.Settings([profile], "p"))
        self.feed(view, "ab\x1b[1;1H")
        self.assertEqual(self.pixel(view, 0, 0, 0.05, 0.05), QColor(view.scheme().cursor))
        self.feed(view, "\x1b[?25l")
        self.assertEqual(self.pixel(view, 0, 0, 0.05, 0.05), view.background())
        self.feed(view, "\x1b[?25h\x1b[4 q")
        self.assertEqual(self.pixel(view, 0, 0, 0.05, 0.05), view.background())
        self.assertEqual(self.pixel(view, 0, 0, 0.5, 0.97), QColor(view.scheme().cursor))

    def test_cursor_blinks_only_while_focused_and_visible(self):
        view = self.view()
        self.assertTrue(view.hasFocus())
        self.assertTrue(view.is_blinking())
        view.clearFocus()
        testing.process()
        self.assertFalse(view.is_blinking())
        view.setFocus()
        testing.process()
        self.assertTrue(view.is_blinking())
        self.feed(view, "\x1b[2 q")                       # the program asks for a steady block
        view._sync_blink()
        self.assertFalse(view.is_blinking())
        self.feed(view, "\x1b[0 q")
        view._sync_blink()
        self.assertTrue(view.is_blinking())
        view.hide()
        testing.process()
        self.assertFalse(view.is_blinking())

    def test_scheme_follows_the_desktop(self):
        view = self.view(text="x")
        replies = []
        view.send.connect(replies.append)
        self.feed(view, "\x1b[?2031h")
        dark = view.background()
        testing.set_scheme("light")
        view.flush()
        self.assertNotEqual(view.background(), dark)
        self.assertEqual(view.background(), QColor(schemes.prism("light").background))
        self.assertEqual(self.pixel(view, 30, 5), view.background())
        self.assertIn(b"\x1b[?997;2n", replies)
        self.feed(view, "\x1b]11;?\x07")
        colour = view.background()
        self.assertIn(f"\x1b]11;rgb:{colour.red() * 257:04x}/{colour.green() * 257:04x}/{colour.blue() * 257:04x}\x07"
                      .encode(), replies)

    def test_fixed_scheme_does_not_follow(self):
        view = self.view()
        profile = settings_module.Profile("p", "P", scheme="solarized-dark")
        view.configure(profile, settings_module.Settings([profile], "p"))
        self.assertEqual(view.background(), QColor(schemes.classic()["solarized-dark"].background))
        testing.set_scheme("light")
        self.assertEqual(view.background(), QColor(schemes.classic()["solarized-dark"].background))

    def test_program_colours(self):
        view = self.view(text="\x1b]11;#102030\x07\x1b]4;1;#00ff00\x07\x1b[41m ")
        self.assertEqual(view.background(), QColor(16, 32, 48))
        self.assertEqual(self.pixel(view, 0, 0), QColor(0, 255, 0))
        self.feed(view, "\x1b]111\x07")
        self.assertEqual(view.background(), QColor(view.scheme().background))

    def test_bell(self):
        view = self.view()
        rang = []
        view.bell_rang.connect(lambda: rang.append(1))
        self.feed(view, "\x07\x07\x07")
        self.assertEqual(rang, [1])                       # a burst of bells is one bell
        self.assertTrue(view.is_flashing())
        self.assertTrue(testing.wait_until(lambda: not view.is_flashing(), 2))
        fluent.manager().set_reduced_motion(True)
        try:
            view._last_bell = 0
            self.feed(view, "\x07")
            self.assertFalse(view.is_flashing())
        finally:
            fluent.manager().set_reduced_motion(None)

    def test_title_and_directory(self):
        view = self.view()
        titles, directories = [], []
        view.title_changed.connect(titles.append)
        view.cwd_changed.connect(directories.append)
        self.feed(view, "\x1b]0;hello\x07\x1b]7;file:///tmp\x07")
        self.assertEqual((titles, directories), (["hello"], ["/tmp"]))

    def test_zoom(self):
        view = self.view()
        cell = view.cell_size()
        cols = view.grid_size()[0]
        view.zoom_in()
        view.zoom_in()
        self.assertGreater(view.cell_size()[0], cell[0])
        self.assertLess(view.grid_size()[0], cols)
        self.assertEqual(view.font_points(), 13)
        view.zoom_reset()
        self.assertEqual(view.cell_size(), cell)
        for _ in range(100):
            view.zoom_out()
        self.assertEqual(view.font_points(), settings_module.FONT_SIZE_MIN)

    def test_resize_keeps_the_content(self):
        view = self.view(640, 360, "".join(f"line {i}\r\n" for i in range(60)) + "a line that is rather long " * 3)
        sizes = []
        view.grid_resized.connect(lambda cols, rows: sizes.append((cols, rows)))
        before = [view.term.line_text(i) for i in range(view.term.total_lines())]
        view.resize(300, 200)
        testing.process()
        self.assertEqual(sizes[-1], view.grid_size())
        self.assertLess(view.grid_size()[0], 40)
        view.resize(640, 360)
        testing.process()
        view.flush()
        after = [view.term.line_text(i) for i in range(view.term.total_lines())]
        self.assertEqual([line for line in after if line], [line for line in before if line])

    def test_tiny_and_huge_sizes(self):
        view = self.view(640, 360, "text")
        for size in ((1, 1), (20, 10), (3000, 2000), (640, 360)):
            view.resize(*size)
            testing.process()
            view.flush()
        self.assertEqual(view.term.line_text(0), "text")

    def test_redraw_time_of_a_large_grid(self):
        view = self.view(640, 360)
        cell_w, cell_h = view.cell_size()
        size = view.size_for(200, 60)
        view.resize(size)
        testing.process()
        self.assertEqual(view.grid_size(), (200, 60))
        lines = []
        for row in range(60):
            parts = [f"\x1b[{31 + (row + n) % 7}m{'word%02d' % n}\x1b[0m file_{row:03d}.txt  " for n in range(9)]
            lines.append(("".join(parts))[:1000])
        self.feed(view, "\r\n".join(lines)[:-1])
        timings = []
        for _ in range(10):
            view._full = True
            started = time.perf_counter()
            view.flush()
            timings.append(time.perf_counter() - started)
        best = min(timings) * 1000
        print(f"\n    full redraw of a 200x60 grid: {best:.1f} ms (median {sorted(timings)[5] * 1000:.1f} ms)", end=" ")
        self.assertLess(best, 100)


class Keyboard(ViewCase):
    def test_typing(self):
        view = self.view()
        testing.type_text(view, "ls -l")
        testing.key(view, Qt.Key.Key_Return, text="\r")
        self.assertEqual(b"".join(self.sent), b"ls -l\r")

    def test_special_keys_follow_the_modes(self):
        view = self.view()
        testing.key(view, Qt.Key.Key_Up)
        testing.key(view, Qt.Key.Key_F5, SHIFT)
        testing.key(view, Qt.Key.Key_Tab, text="\t")
        testing.key(view, Qt.Key.Key_Backtab, SHIFT)
        testing.key(view, Qt.Key.Key_Backspace, text="\x08")
        self.feed(view, "\x1b[?1h\x1b=")
        testing.key(view, Qt.Key.Key_Up)
        testing.key(view, Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier, "\r")
        testing.key(view, Qt.Key.Key_5, Qt.KeyboardModifier.KeypadModifier, "5")
        self.assertEqual(self.sent, [b"\x1b[A", b"\x1b[15;2~", b"\t", b"\x1b[Z", b"\x7f", b"\x1bOA", b"\x1bOM", b"5"])

    def test_control_and_alt(self):
        view = self.view()
        testing.key(view, Qt.Key.Key_C, CTRL, "\x03")
        testing.key(view, Qt.Key.Key_BracketLeft, CTRL, "\x1b")
        testing.key(view, Qt.Key.Key_Space, CTRL, " ")
        testing.key(view, Qt.Key.Key_F, ALT, "f")
        testing.key(view, Qt.Key.Key_Left, CTRL)
        testing.key(view, Qt.Key.Key_Control, CTRL)
        testing.key(view, Qt.Key.Key_Shift, SHIFT)
        self.assertEqual(self.sent, [b"\x03", b"\x1b", b"\x00", b"\x1bf", b"\x1b[1;5D"])

    def test_typing_goes_back_to_the_bottom(self):
        view = self.view(text="".join(f"{i}\r\n" for i in range(100)))
        view.scroll_lines(10)
        self.assertEqual(view.scroll_offset(), 10)
        testing.type_text(view, "x")
        self.assertEqual(view.scroll_offset(), 0)

    def test_scrolling_keys(self):
        view = self.view(text="".join(f"{i}\r\n" for i in range(200)))
        rows = view.grid_size()[1]
        testing.key(view, Qt.Key.Key_PageUp, SHIFT)
        self.assertEqual(view.scroll_offset(), rows - 1)
        testing.key(view, Qt.Key.Key_PageDown, SHIFT)
        self.assertEqual(view.scroll_offset(), 0)
        testing.key(view, Qt.Key.Key_Up, CTRL | SHIFT)
        self.assertEqual(view.scroll_offset(), 1)
        testing.key(view, Qt.Key.Key_Home, CTRL | SHIFT)
        self.assertEqual(view.scroll_offset(), len(view.term.history))
        testing.key(view, Qt.Key.Key_End, CTRL | SHIFT)
        self.assertEqual(view.scroll_offset(), 0)
        self.assertEqual(self.sent, [])

    def test_input_method(self):
        view = self.view()
        event = QInputMethodEvent("にほ", [])
        QGuiApplication.sendEvent(view, event)
        view.flush()
        self.assertEqual(self.sent, [])
        event = QInputMethodEvent()
        event.setCommitString("日本")
        QGuiApplication.sendEvent(view, event)
        self.assertEqual(self.sent, ["日本".encode()])
        self.assertTrue(view.inputMethodQuery(Qt.InputMethodQuery.ImCursorRectangle).isValid())

    def test_focus_reports(self):
        view = self.view(text="\x1b[?1004h")
        view.clearFocus()
        view.setFocus()
        testing.process()
        self.assertEqual(self.sent, [b"\x1b[O", b"\x1b[I"])


class MouseAndSelection(ViewCase):
    TEXT = "alpha beta gamma\r\nsecond line here\r\nthird"

    def test_drag_selects_characters(self):
        view = self.view(text=self.TEXT)
        drag(view, cell_point(view, 6, 0), cell_point(view, 9, 0))
        self.assertEqual(view.selected_text(), "beta")
        drag(view, cell_point(view, 11, 0), cell_point(view, 5, 1))
        self.assertEqual(view.selected_text(), "gamma\nsecond")
        self.assertEqual(self.sent, [])

    def test_click_without_drag_selects_nothing(self):
        view = self.view(text=self.TEXT)
        drag(view, cell_point(view, 0, 0), cell_point(view, 3, 0))
        testing.click(view, cell_point(view, 8, 1))
        self.assertFalse(view.has_selection())

    def test_double_and_triple_click(self):
        view = self.view(text=self.TEXT)
        point = cell_point(view, 7, 0)
        testing.click(view, point)
        mouse(view, "double", point)
        mouse(view, "release", point)
        self.assertEqual(view.selected_text(), "beta")
        mouse(view, "press", point)
        mouse(view, "release", point)
        self.assertEqual(view.selected_text(), "alpha beta gamma")

    def test_double_click_drag_extends_by_words(self):
        view = self.view(text=self.TEXT)
        point = cell_point(view, 7, 0)
        testing.click(view, point)
        mouse(view, "double", point)
        mouse(view, "move", cell_point(view, 12, 0), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
        mouse(view, "release", cell_point(view, 12, 0))
        self.assertEqual(view.selected_text(), "beta gamma")

    def test_shift_click_extends(self):
        view = self.view(text=self.TEXT)
        testing.click(view, cell_point(view, 0, 0))
        testing.click(view, cell_point(view, 4, 1), modifiers=SHIFT)
        self.assertEqual(view.selected_text(), "alpha beta gamma\nsecon")

    def test_alt_drag_selects_a_block(self):
        view = self.view(text=self.TEXT)
        drag(view, cell_point(view, 1, 0), cell_point(view, 3, 2), ALT)
        self.assertEqual(view.selected_text(), "lph\neco\nhir")
        self.assertTrue(view.selection().block)

    def test_selection_goes_to_the_primary_selection_and_optionally_the_clipboard(self):
        view = self.view(text=self.TEXT)
        clipboard = QGuiApplication.clipboard()
        clipboard.setText("before")
        drag(view, cell_point(view, 0, 0), cell_point(view, 4, 0))
        self.assertEqual(clipboard.text(), "before")
        view.copy_on_select = True
        drag(view, cell_point(view, 6, 0), cell_point(view, 9, 0))
        self.assertEqual(clipboard.text(), "beta")

    def test_copy(self):
        view = self.view(text=self.TEXT)
        clipboard = QGuiApplication.clipboard()
        clipboard.setText("before")
        self.assertFalse(view.copy())
        drag(view, cell_point(view, 0, 2), cell_point(view, 4, 2))
        self.assertTrue(view.copy())
        self.assertEqual(clipboard.text(), "third")

    def test_ctrl_c_copies_a_selection_and_interrupts_otherwise(self):
        view = self.view(text=self.TEXT)
        clipboard = QGuiApplication.clipboard()
        drag(view, cell_point(view, 0, 0), cell_point(view, 4, 0))
        testing.key(view, Qt.Key.Key_C, CTRL, "\x03")
        self.assertEqual(clipboard.text(), "alpha")
        self.assertFalse(view.has_selection())
        self.assertEqual(self.sent, [])
        testing.key(view, Qt.Key.Key_C, CTRL, "\x03")
        self.assertEqual(self.sent, [b"\x03"])
        view.ctrl_c_copies = False
        drag(view, cell_point(view, 0, 0), cell_point(view, 4, 0))
        testing.key(view, Qt.Key.Key_C, CTRL, "\x03")
        self.assertEqual(self.sent, [b"\x03", b"\x03"])

    def test_select_all(self):
        view = self.view(text=self.TEXT)
        view.select_all()
        self.assertEqual(view.selected_text(), "alpha beta gamma\nsecond line here\nthird")

    def test_selection_survives_scrolling_but_not_a_change_under_it(self):
        view = self.view(text=self.TEXT)
        drag(view, cell_point(view, 0, 1), cell_point(view, 5, 1))
        self.assertEqual(view.selected_text(), "second")
        self.feed(view, "\r\n" * 60)
        self.assertEqual(view.selected_text(), "second")
        view.clear_selection()
        self.feed(view, "\x1b[2J\x1b[Hfresh text")
        drag(view, cell_point(view, 0, 0), cell_point(view, 4, 0))
        self.assertEqual(view.selected_text(), "fresh")
        self.feed(view, "\x1b[HFRESH")
        self.assertFalse(view.has_selection())

    def test_selection_in_the_scrollback(self):
        view = self.view(text="".join(f"row {i}\r\n" for i in range(100)))
        rows = view.grid_size()[1]
        view.scroll_to_top()
        view.flush()
        drag(view, cell_point(view, 0, 0), cell_point(view, 4, 1))
        self.assertEqual(view.selected_text(), "row 0\nrow 1")
        view.scroll_to_bottom()
        view.flush()
        self.assertEqual(view.selected_text(), "row 0\nrow 1")
        self.assertGreater(rows, 2)

    def test_middle_button_pastes(self):
        view = self.view()
        clipboard = QGuiApplication.clipboard()
        clipboard.setText("pasted")
        if clipboard.supportsSelection():
            clipboard.setText("primary", clipboard.Mode.Selection)
        testing.click(view, cell_point(view, 2, 2), button=Qt.MouseButton.MiddleButton)
        self.assertEqual(self.sent, [b"primary" if clipboard.supportsSelection() else b"pasted"])

    def test_paste_and_bracketed_paste(self):
        view = self.view()
        QGuiApplication.clipboard().setText("echo a\necho b\x1b[201~")
        view.paste()
        self.feed(view, "\x1b[?2004h")
        testing.key(view, Qt.Key.Key_Insert, SHIFT)
        self.assertEqual(self.sent, [b"echo a\recho b[201~", b"\x1b[200~echo a\recho b[201~\x1b[201~"])

    def test_paste_guard(self):
        view = self.view()
        asked = []
        view.paste_guard = lambda text: (asked.append(text), False)[1]
        view.paste_text("no")
        self.assertEqual((asked, self.sent), (["no"], []))

    def test_wheel_scrolls_the_scrollback(self):
        view = self.view(text="".join(f"{i}\r\n" for i in range(200)))
        wheel(view, QPoint(50, 50), 1)
        self.assertEqual(view.scroll_offset(), 3)
        wheel(view, QPoint(50, 50), -1)
        self.assertEqual(view.scroll_offset(), 0)
        wheel(view, QPoint(50, 50), 1000)
        self.assertEqual(view.scroll_offset(), len(view.term.history))
        self.assertEqual(self.sent, [])

    def test_scrollbar(self):
        view = self.view()
        self.assertFalse(view.scrollbar().isVisible())
        self.feed(view, "".join(f"{i}\r\n" for i in range(200)))
        bar = view.scrollbar()
        self.assertTrue(bar.isVisible())
        self.assertEqual((bar.maximum(), bar.value()), (len(view.term.history), len(view.term.history)))
        bar.setValue(10)
        self.assertEqual(view.scroll_offset(), len(view.term.history) - 10)
        self.feed(view, "\x1b[?1049h")
        self.assertFalse(bar.isVisible())

    def test_view_stays_on_its_lines_while_output_arrives(self):
        view = self.view(text="".join(f"row {i}\r\n" for i in range(100)))
        view.scroll_lines(20)
        view.flush()
        shown = view.term.line_text(view._first)
        self.feed(view, "".join(f"more {i}\r\n" for i in range(30)))
        self.assertEqual(view.term.line_text(view._first), shown)
        self.assertEqual(view.scroll_offset(), 50)

    def test_wheel_in_a_full_screen_program_sends_arrow_keys(self):
        view = self.view(text="\x1b[?1049h")
        wheel(view, QPoint(50, 50), 1)
        wheel(view, QPoint(50, 50), -1)
        self.assertEqual(self.sent, [b"\x1b[A" * 3, b"\x1b[B" * 3])

    def test_control_wheel_zooms(self):
        view = self.view()
        wheel(view, QPoint(50, 50), 1, CTRL)
        self.assertEqual(view.zoom(), 1)

    def test_mouse_reporting(self):
        view = self.view(text="\x1b[?1002h\x1b[?1006h")
        point, other = cell_point(view, 4, 2), cell_point(view, 6, 3)
        mouse(view, "press", point)
        mouse(view, "move", other, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
        mouse(view, "release", other)
        wheel(view, point, 1)
        wheel(view, point, -1)
        testing.click(view, point, button=Qt.MouseButton.RightButton)
        self.assertEqual(self.sent, [b"\x1b[<0;5;3M", b"\x1b[<32;7;4M", b"\x1b[<0;7;4m", b"\x1b[<64;5;3M",
                                     b"\x1b[<65;5;3M", b"\x1b[<2;5;3M", b"\x1b[<2;5;3m"])
        self.assertFalse(view.has_selection())

    def test_shift_selects_while_a_program_uses_the_mouse(self):
        view = self.view(text="\x1b[?1000hsome text")
        drag(view, cell_point(view, 0, 0), cell_point(view, 3, 0), SHIFT)
        self.assertEqual(view.selected_text(), "some")
        self.assertEqual(self.sent, [])

    def test_x10_and_motion_modes(self):
        view = self.view(text="\x1b[?9h")
        testing.click(view, cell_point(view, 0, 0))
        self.assertEqual(self.sent, [b"\x1b[M !!"])
        self.sent.clear()
        self.feed(view, "\x1b[?9l\x1b[?1003h\x1b[?1006h")
        mouse(view, "move", cell_point(view, 2, 1), Qt.MouseButton.NoButton, Qt.MouseButton.NoButton)
        mouse(view, "move", cell_point(view, 2, 1), Qt.MouseButton.NoButton, Qt.MouseButton.NoButton)
        self.assertEqual(self.sent, [b"\x1b[<35;3;2M"])


class Links(ViewCase):
    def test_written_address(self):
        view = self.view(text="see https://example.org/page now")
        opened = []
        view.link_activated.connect(opened.append)
        mouse(view, "move", cell_point(view, 8, 0), Qt.MouseButton.NoButton, Qt.MouseButton.NoButton)
        self.assertEqual(view.hovered_link(), "https://example.org/page")
        self.assertIn("Ctrl+click", view.toolTip())
        testing.click(view, cell_point(view, 8, 0))
        self.assertEqual(opened, [])
        testing.click(view, cell_point(view, 8, 0), modifiers=CTRL)
        self.assertEqual(opened, ["https://example.org/page"])
        mouse(view, "move", cell_point(view, 1, 0), Qt.MouseButton.NoButton, Qt.MouseButton.NoButton)
        self.assertEqual(view.hovered_link(), "")

    def test_hyperlink_sequence(self):
        view = self.view(text="\x1b]8;;https://example.org/hidden\x07click here\x1b]8;;\x07 x")
        self.assertEqual(view.link_at(3, 0)[0], "https://example.org/hidden")
        self.assertEqual(view.link_at(3, 0)[1], [(0, 0, 10)])
        self.assertEqual(view.link_at(11, 0), ("", []))

    def test_hover_underlines(self):
        view = self.view(text="https://example.org")
        cell_w, cell_h = view.cell_size()

        def underline():
            y = view._pad_y + view._underline_y
            return sum(view._image.pixel(view._pad_x + x, y) != view.background().rgb() for x in range(19 * cell_w))

        before = underline()
        mouse(view, "move", cell_point(view, 3, 0), Qt.MouseButton.NoButton, Qt.MouseButton.NoButton)
        view.flush()
        self.assertEqual(underline(), 19 * cell_w)
        self.assertLess(before, 19 * cell_w)

    def test_context_menu_request_carries_the_link(self):
        view = self.view(text="https://example.org")
        requests = []
        view.menu_requested.connect(lambda position, link: requests.append(link))
        from PySide6.QtGui import QContextMenuEvent
        point = cell_point(view, 2, 0)
        QGuiApplication.sendEvent(view, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point,
                                                          view.mapToGlobal(point)))
        self.assertEqual(requests, ["https://example.org"])


class Clipboard(ViewCase):
    def test_programs_write_the_clipboard_only_with_permission(self):
        view = self.view()
        denied = []
        view.clipboard_denied.connect(lambda: denied.append(1))
        clipboard = QGuiApplication.clipboard()
        clipboard.setText("mine")
        payload = base64.b64encode(b"theirs").decode()
        self.feed(view, f"\x1b]52;c;{payload}\x07")
        self.assertEqual((clipboard.text(), denied), ("mine", [1]))
        profile = settings_module.Profile("p", "P")
        view.configure(profile, settings_module.Settings([profile], "p", clipboard_write=True))
        self.feed(view, f"\x1b]52;c;{payload}\x07\x1b]52;c;?\x07")
        self.assertEqual(clipboard.text(), "theirs")
        self.assertEqual(self.sent, [])


class Scaling(unittest.TestCase):
    SCRIPT = r"""
import sys
from boswas_fluent import testing
app = testing.app("dark")
from PySide6.QtGui import QColor
from boswas_apps.terminal.view import TerminalView
view = TerminalView()
view.resize(500, 300); view.show(); testing.process()
view.feed(("\x1b[7m" + " " * 400 + "\x1b[0m\r\n" + "M" * 40 + "\r\n" + "─" * 40).encode()); view.flush()
ratio = view.devicePixelRatioF()
cell_w, cell_h = view.cell_size()
cols, rows = view.grid_size()
assert isinstance(cell_w, int) and isinstance(cell_h, int), (cell_w, cell_h)
shot = view.grab().toImage()
assert abs(shot.devicePixelRatio() - ratio) < 1e-6, shot.devicePixelRatio()
foreground = view.foreground().rgb()
pad = view._pad_x
# the inverse bar: every device pixel of the first row has the foreground colour, on screen as in the image
for name, image in (("image", view._image), ("screen", shot)):
    bad = [(x, y) for y in range(pad, pad + cell_h) for x in range(pad, pad + cols * cell_w)
           if image.pixel(x, y) != foreground]
    assert not bad, (name, len(bad), bad[:5])
# what is on screen is the image, pixel for pixel
different = sum(shot.pixel(x, y) != view._image.pixel(x, y) for y in range(0, min(shot.height(), view._image.height()))
                for x in range(0, min(shot.width(), view._image.width()), 3))
assert different == 0, different
# glyphs sit on the grid: the same letter looks the same in every cell
def cell(column):
    return [view._image.pixel(pad + column * cell_w + x, pad + cell_h + y) for y in range(cell_h) for x in range(cell_w)]
assert all(cell(column) == cell(0) for column in range(1, 40)), "glyphs drift"
print("ratio", ratio, "cell", cell_w, cell_h, "grid", cols, rows)
"""

    def run_scaled(self, factor):
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=str(factor),
                           QT_ENABLE_HIGHDPI_SCALING="1", QT_SCALE_FACTOR_ROUNDING_POLICY="PassThrough")
        return subprocess.run([sys.executable, "-B", "-c", self.SCRIPT], env=environment, capture_output=True,
                              text=True, timeout=120)

    def test_fractional_scale_factors_have_no_seams(self):
        for factor in (1, 1.25, 1.5, 1.75, 2):
            result = self.run_scaled(factor)
            self.assertEqual(result.returncode, 0, f"scale {factor}: {result.stderr[-1500:]}")
            self.assertIn(f"ratio {float(factor)}", result.stdout)


if __name__ == "__main__":
    unittest.main()
