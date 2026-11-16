"""The emulator against byte streams: what ends up on the screen, in the scrollback and in the answers."""

import base64
import random
import unittest

from boswas_apps.terminal import emulator
from boswas_apps.terminal.emulator import (BOLD, DEFAULT_ATTR, DIM, INVERSE, ITALIC, PALETTE, RGB, STRIKE,
                                           UNDERLINE_MASK, UNDERLINE_SHIFT, Terminal)


def terminal(cols=20, rows=5, scrollback=100):
    term = Terminal(cols, rows, scrollback)
    term.replies = []
    term.on_reply = term.replies.append
    return term


def feed(term, text):
    term.feed(text.encode("utf-8") if isinstance(text, str) else text)
    return term


def attr_at(term, row, column):
    return term.lines[row].attrs[column]


class Printing(unittest.TestCase):
    def test_plain_text_and_line_ends(self):
        term = feed(terminal(), "hello\r\nworld")
        self.assertEqual(term.screen_text()[:2], ["hello", "world"])
        self.assertEqual((term.x, term.y), (5, 1))

    def test_line_feed_keeps_the_column(self):
        term = feed(terminal(), "ab\ncd")
        self.assertEqual(term.screen_text()[:2], ["ab", "  cd"])

    def test_wrap_is_deferred_until_the_next_character(self):
        term = feed(terminal(cols=5), "abcde")
        self.assertEqual((term.x, term.y, term.wrapnext), (4, 0, True))
        feed(term, "f")
        self.assertEqual(term.screen_text()[:2], ["abcde", "f"])
        self.assertTrue(term.lines[0].wrapped)

    def test_carriage_return_after_last_column_stays_on_the_line(self):
        term = feed(terminal(cols=5), "abcde\rX")
        self.assertEqual(term.screen_text()[0], "Xbcde")

    def test_no_autowrap_overwrites_the_last_column(self):
        term = feed(terminal(cols=5), "\x1b[?7labcdefgh")
        self.assertEqual(term.screen_text()[:2], ["abcdh", ""])

    def test_backspace_tab_and_bell(self):
        term = terminal()
        bells = []
        term.on_bell = lambda: bells.append(1)
        feed(term, "a\tb\x08c\x07")
        self.assertEqual(term.screen_text()[0], "a       c")
        self.assertEqual(bells, [1])

    def test_tab_stops_can_be_set_and_cleared(self):
        term = feed(terminal(), "\x1b[3g\x1b[1;5H\x1bH\r\tX")
        self.assertEqual(term.screen_text()[0], "    X")
        feed(term, "\r\x1b[2K\x1b[I\x1b[IY")           # no more stops: the last column
        self.assertEqual(term.x, 19)

    def test_utf8_split_across_chunks(self):
        term = terminal()
        data = "héllo → ✓".encode("utf-8")
        for index in range(len(data)):
            term.feed(data[index:index + 1])
        self.assertEqual(term.screen_text()[0], "héllo → ✓")

    def test_invalid_utf8_becomes_replacement_characters(self):
        term = feed(terminal(), b"a\xff\xfeb\xc3")
        self.assertEqual(term.screen_text()[0], "a��b")

    def test_insert_mode(self):
        term = feed(terminal(), "abc\r\x1b[4hXY\x1b[4l")
        self.assertEqual(term.screen_text()[0], "XYabc")

    def test_repeat(self):
        term = feed(terminal(), "ab\x1b[3b")
        self.assertEqual(term.screen_text()[0], "abbbb")

    def test_dec_special_graphics(self):
        term = feed(terminal(), "\x1b(0lqk\x1b(Blqk \x0e\x1b)0x\x0fx")
        self.assertEqual(term.screen_text()[0], "┌─┐lqk │x")


class WideAndCombining(unittest.TestCase):
    def test_wide_characters_take_two_cells(self):
        term = feed(terminal(), "a日本b")
        self.assertEqual(term.lines[0].chars[:7], ["a", "日", "", "本", "", "b", " "])
        self.assertEqual(term.x, 6)

    def test_emoji_is_wide(self):
        term = feed(terminal(), "👍x")
        self.assertEqual(term.lines[0].chars[:3], ["👍", "", "x"])

    def test_combining_marks_join_their_base(self):
        term = feed(terminal(), "éx")
        self.assertEqual(term.lines[0].chars[:2], ["é", "x"])
        self.assertEqual(term.x, 2)

    def test_indic_text_keeps_the_grid(self):
        term = feed(terminal(), "क्षि|")
        chars = term.lines[0].chars
        self.assertEqual(chars[:4], ["क्", "ष", "ि", "|"])
        self.assertEqual(len(chars), 20)
        self.assertEqual(term.x, 4)

    def test_zero_width_joiner_sequences(self):
        term = feed(terminal(), "👨‍👩‍👧|")
        self.assertEqual(term.lines[0].chars[:7], ["👨‍", "", "👩‍", "", "👧", "", "|"])

    def test_combining_at_the_start_of_a_line_is_dropped(self):
        term = feed(terminal(), "́a")
        self.assertEqual(term.lines[0].chars[0], "a")

    def test_combining_sequences_are_capped(self):
        term = feed(terminal(), "a" + "́" * 500)
        self.assertLessEqual(len(term.lines[0].chars[0]), emulator.MAX_CLUSTER)

    def test_wide_character_wraps_whole(self):
        term = feed(terminal(cols=5), "abcd日")
        self.assertEqual(term.screen_text()[:2], ["abcd", "日"])
        self.assertTrue(term.lines[0].wrapped)

    def test_overwriting_half_of_a_wide_character_blanks_the_other_half(self):
        term = feed(terminal(), "日本\x1b[1;2Hx")
        self.assertEqual(term.lines[0].chars[:4], [" ", "x", "本", ""])
        term = feed(terminal(), "日本\x1b[1;1Hx")
        self.assertEqual(term.lines[0].chars[:4], ["x", " ", "本", ""])

    def test_editing_never_leaves_half_a_wide_character(self):
        term = feed(terminal(cols=6), "日本語\x1b[1;2H\x1b[P")
        chars = term.lines[0].chars
        for index, cell in enumerate(chars):
            if cell == "":
                self.assertTrue(index > 0 and emulator._is_wide(chars[index - 1]), chars)
        term = feed(terminal(cols=6), "日本語\x1b[1;1H\x1b[@")
        chars = term.lines[0].chars
        self.assertEqual(len(chars), 6)
        self.assertFalse(emulator._is_wide(chars[-1]))

    def test_grid_shape_survives_mixed_text(self):
        term = terminal(cols=7, rows=4)
        feed(term, "日本語テキストabć日本\r\n\x1b[2;3H\x1b[2P\x1b[3@日\x1b[1K\x1b[X")
        for line in term.lines:
            self.assertEqual(len(line.chars), 7)
            self.assertEqual(len(line.attrs), 7)


class CursorAndEditing(unittest.TestCase):
    def test_cursor_addressing(self):
        term = feed(terminal(), "\x1b[3;7HX\x1b[HY\x1b[2;2fZ\x1b[4GQ\x1b[4dR")
        self.assertEqual(term.screen_text(), ["Y", " Z Q", "      X", "    R", ""])

    def test_relative_movement_is_clamped(self):
        term = feed(terminal(), "\x1b[99B\x1b[99C")
        self.assertEqual((term.x, term.y), (19, 4))
        feed(term, "\x1b[99A\x1b[99D")
        self.assertEqual((term.x, term.y), (0, 0))
        feed(term, "\x1b[2B\x1b[3C\x1b[E")
        self.assertEqual((term.x, term.y), (0, 3))
        feed(term, "\x1b[5C\x1b[2F")
        self.assertEqual((term.x, term.y), (0, 1))

    def test_erase_in_line_and_display(self):
        term = feed(terminal(), "abcdef\r\nghijkl\r\nmnopqr\x1b[2;4H\x1b[K")
        self.assertEqual(term.screen_text()[:3], ["abcdef", "ghi", "mnopqr"])
        feed(term, "\x1b[1K")
        self.assertEqual(term.screen_text()[1], "")
        feed(term, "\x1b[3;3H\x1b[1J")
        self.assertEqual(term.screen_text()[:3], ["", "", "   pqr"])
        feed(term, "\x1b[J")
        self.assertEqual(term.screen_text()[2], "")
        feed(term, "x\x1b[2J")
        self.assertEqual(term.screen_text(), [""] * 5)

    def test_erase_uses_the_background_colour(self):
        term = feed(terminal(), "\x1b[41m\x1b[2Kx\x1b[0m")
        self.assertEqual(attr_at(term, 0, 10), (0, PALETTE | 1, 0, 0, 0))

    def test_insert_and_delete_characters(self):
        term = feed(terminal(), "abcdef\x1b[1;3H\x1b[2@")
        self.assertEqual(term.screen_text()[0], "ab  cdef")
        feed(term, "\x1b[3P")
        self.assertEqual(term.screen_text()[0], "abdef")
        feed(term, "\x1b[2X")
        self.assertEqual(term.screen_text()[0], "ab  f")

    def test_insert_and_delete_lines(self):
        term = feed(terminal(), "1\r\n2\r\n3\r\n4\r\n5\x1b[2;1H\x1b[2L")
        self.assertEqual(term.screen_text(), ["1", "", "", "2", "3"])
        feed(term, "\x1b[1M")
        self.assertEqual(term.screen_text(), ["1", "", "2", "3", ""])

    def test_scroll_region(self):
        term = feed(terminal(), "1\r\n2\r\n3\r\n4\r\n5\x1b[2;4r\x1b[4;1H\n")
        self.assertEqual(term.screen_text(), ["1", "3", "4", "", "5"])
        self.assertEqual(len(term.history), 0)
        feed(term, "\x1b[2;1H\x1bM")
        self.assertEqual(term.screen_text(), ["1", "", "3", "4", "5"])
        feed(term, "\x1b[2S")
        self.assertEqual(term.screen_text(), ["1", "4", "", "", "5"])
        feed(term, "\x1b[1T")
        self.assertEqual(term.screen_text(), ["1", "", "4", "", "5"])
        feed(term, "\x1b[r\x1b[5;1H\n")
        self.assertEqual(len(term.history), 1)

    def test_origin_mode(self):
        term = feed(terminal(), "\x1b[2;4r\x1b[?6h\x1b[1;1HX\x1b[9;1HY\x1b[6n")
        self.assertEqual(term.screen_text()[1], "X")
        self.assertEqual(term.screen_text()[3], "Y")
        self.assertEqual(term.replies[-1], b"\x1b[3;2R")

    def test_index_and_next_line(self):
        term = feed(terminal(), "ab\x1bDc\x1bEd")
        self.assertEqual(term.screen_text()[:3], ["ab", "  c", "d"])

    def test_save_and_restore_cursor(self):
        term = feed(terminal(), "\x1b[2;3H\x1b[31m\x1b7\x1b[H\x1b[0mA\x1b8B")
        self.assertEqual(term.screen_text()[:2], ["A", "  B"])
        self.assertEqual(attr_at(term, 1, 2)[0], PALETTE | 1)
        feed(term, "\x1b[4;4H\x1b[s\x1b[H\x1b[uC")
        self.assertEqual(term.screen_text()[3], "   C")

    def test_scrolling_fills_the_scrollback(self):
        term = feed(terminal(rows=3, scrollback=4), "".join(f"line{i}\r\n" for i in range(10)))
        self.assertEqual(len(term.history), 4)
        self.assertEqual([term.line_text(i) for i in range(4)], ["line4", "line5", "line6", "line7"])
        self.assertEqual(term.history.dropped, 4)
        feed(term, "\x1b[3J")
        self.assertEqual(len(term.history), 0)

    def test_scrollback_can_be_off(self):
        term = feed(terminal(rows=2, scrollback=0), "a\r\nb\r\nc\r\nd")
        self.assertEqual(len(term.history), 0)
        self.assertEqual(term.screen_text(), ["c", "d"])

    def test_scrollback_memory_is_bounded(self):
        term = Terminal(40, 4, 100000)
        term.history.max_bytes = 200000
        row = "".join(f"\x1b[38;2;{i};{i * 2 % 256};{i * 3 % 256}m{chr(0x4e00 + i)}" for i in range(20))
        for _ in range(3000):
            term.feed((row + "\r\n").encode())
        self.assertLessEqual(term.history.cost, 200000)
        self.assertLess(len(term.history), 3000)


class Attributes(unittest.TestCase):
    def test_basic_and_bright_colours(self):
        term = feed(terminal(), "\x1b[31;42ma\x1b[91;102mb\x1b[39;49mc")
        self.assertEqual(attr_at(term, 0, 0)[:2], (PALETTE | 1, PALETTE | 2))
        self.assertEqual(attr_at(term, 0, 1)[:2], (PALETTE | 9, PALETTE | 10))
        self.assertEqual(attr_at(term, 0, 2), DEFAULT_ATTR)

    def test_256_and_true_colour(self):
        term = feed(terminal(), "\x1b[38;5;196ma\x1b[48;2;1;2;3mb\x1b[38:2::10:20:30mc\x1b[38:5:17md\x1b[38:2:4:5:6me")
        self.assertEqual(attr_at(term, 0, 0)[0], PALETTE | 196)
        self.assertEqual(attr_at(term, 0, 1)[1], RGB | 0x010203)
        self.assertEqual(attr_at(term, 0, 2)[0], RGB | 0x0A141E)
        self.assertEqual(attr_at(term, 0, 3)[0], PALETTE | 17)
        self.assertEqual(attr_at(term, 0, 4)[0], RGB | 0x040506)

    def test_styles(self):
        term = feed(terminal(), "\x1b[1;2;3;7;9ma\x1b[22;23;27;29mb\x1b[4mc\x1b[4:3md\x1b[21me\x1b[24mf")
        self.assertEqual(attr_at(term, 0, 0)[2], BOLD | DIM | ITALIC | INVERSE | STRIKE)
        self.assertEqual(attr_at(term, 0, 1), DEFAULT_ATTR)
        self.assertEqual((attr_at(term, 0, 2)[2] & UNDERLINE_MASK) >> UNDERLINE_SHIFT, 1)
        self.assertEqual((attr_at(term, 0, 3)[2] & UNDERLINE_MASK) >> UNDERLINE_SHIFT, 3)
        self.assertEqual((attr_at(term, 0, 4)[2] & UNDERLINE_MASK) >> UNDERLINE_SHIFT, 2)
        self.assertEqual(attr_at(term, 0, 5), DEFAULT_ATTR)

    def test_underline_colour(self):
        term = feed(terminal(), "\x1b[4;58;5;9ma\x1b[58:2::1:2:3mb\x1b[59mc")
        self.assertEqual(attr_at(term, 0, 0)[3], PALETTE | 9)
        self.assertEqual(attr_at(term, 0, 1)[3], RGB | 0x010203)
        self.assertEqual(attr_at(term, 0, 2)[3], 0)

    def test_reset_and_malformed_extended_colour(self):
        term = feed(terminal(), "\x1b[1;31ma\x1b[mb\x1b[38;5mc\x1b[38;9;1md")
        self.assertEqual(attr_at(term, 0, 1), DEFAULT_ATTR)
        self.assertEqual(attr_at(term, 0, 2), DEFAULT_ATTR)
        self.assertEqual(attr_at(term, 0, 3), DEFAULT_ATTR)

    def test_same_attributes_are_one_object(self):
        term = feed(terminal(), "\x1b[31ma\x1b[0m\x1b[31mb")
        self.assertIs(attr_at(term, 0, 0), attr_at(term, 0, 1))

    def test_display_line_runs(self):
        term = feed(terminal(cols=10), "ab\x1b[31mcd\x1b[0mef")
        cells, runs, wrapped = term.display_line(0)
        self.assertEqual("".join(cells).rstrip(), "abcdef")
        self.assertEqual(list(runs), [2, DEFAULT_ATTR, 2, (PALETTE | 1, 0, 0, 0, 0), 6, DEFAULT_ATTR])
        self.assertFalse(wrapped)

    def test_scrollback_keeps_attributes(self):
        term = feed(terminal(cols=10, rows=2), "ab\x1b[31mcd\x1b[0m\r\n\r\n\r\n")
        cells, runs, _wrapped = term.display_line(0)
        self.assertEqual(cells, "abcd")
        self.assertEqual(runs, (2, DEFAULT_ATTR, 2, (PALETTE | 1, 0, 0, 0, 0)))


class Screens(unittest.TestCase):
    def test_alternate_screen_with_saved_cursor(self):
        term = feed(terminal(), "main\x1b[?1049h")
        self.assertTrue(term.alt_active)
        self.assertEqual(term.screen_text(), [""] * 5)
        feed(term, "\x1b[3;3Halt\x1b[31m")
        self.assertEqual(term.screen_text()[2], "  alt")
        feed(term, "\x1b[?1049l")
        self.assertFalse(term.alt_active)
        self.assertEqual(term.screen_text()[0], "main")
        self.assertEqual((term.x, term.y), (4, 0))
        self.assertEqual(term.attr, DEFAULT_ATTR)

    def test_alternate_screen_has_no_scrollback(self):
        term = feed(terminal(rows=3), "\x1b[?1049h" + "x\r\n" * 10)
        self.assertEqual(len(term.history), 0)
        feed(term, "\x1b[?1049l" + "y\r\n" * 5)
        self.assertGreater(len(term.history), 0)

    def test_mode_47_and_1047(self):
        term = feed(terminal(), "main\x1b[?47h")
        self.assertTrue(term.alt_active)
        feed(term, "x\x1b[?47l")
        self.assertEqual(term.screen_text()[0], "main")
        feed(term, "\x1b[?1047h\x1b[?1047l")
        self.assertFalse(term.alt_active)

    def test_full_and_soft_reset(self):
        term = feed(terminal(), "text\x1b[?1h\x1b[?25l\x1b[2;4r\x1b[31m\x1b[4h\x1b[!p")
        self.assertTrue(term.cursor_visible)
        self.assertFalse(term.app_cursor)
        self.assertFalse(term.insert)
        self.assertEqual((term.top, term.bottom), (0, 4))
        self.assertEqual(term.attr, DEFAULT_ATTR)
        self.assertEqual(term.screen_text()[0], "text")
        feed(term, "\x1b[?1049h\x1b[?2004h\x1bc")
        self.assertFalse(term.alt_active)
        self.assertFalse(term.bracketed_paste)
        self.assertEqual(term.screen_text(), [""] * 5)

    def test_decaln(self):
        term = feed(terminal(cols=4, rows=2), "\x1b#8")
        self.assertEqual(term.screen_text(), ["EEEE", "EEEE"])


class Modes(unittest.TestCase):
    def test_modes_are_tracked(self):
        term = feed(terminal(), "\x1b[?1h\x1b=\x1b[?2004h\x1b[?1004h\x1b[?25l\x1b[?1002h\x1b[?1006h\x1b[?12h")
        self.assertTrue(term.app_cursor and term.app_keypad and term.bracketed_paste and term.focus_events)
        self.assertFalse(term.cursor_visible)
        self.assertEqual((term.mouse_mode, term.mouse_encoding), (1002, 1006))
        self.assertIs(term.cursor_blink, True)
        feed(term, "\x1b[?1l\x1b>\x1b[?2004l\x1b[?1004l\x1b[?25h\x1b[?1002l\x1b[?1006l")
        self.assertFalse(term.app_cursor or term.app_keypad or term.bracketed_paste or term.focus_events)
        self.assertEqual((term.mouse_mode, term.mouse_encoding), (0, 0))

    def test_several_modes_in_one_sequence(self):
        term = feed(terminal(), "\x1b[?1000;1006;2004h")
        self.assertEqual((term.mouse_mode, term.mouse_encoding, term.bracketed_paste), (1000, 1006, True))

    def test_cursor_shape(self):
        term = feed(terminal(), "\x1b[5 q")
        self.assertEqual(term.cursor_style, 5)
        feed(term, "\x1b[0 q")
        self.assertEqual(term.cursor_style, 0)
        feed(term, "\x1b[99 q")
        self.assertEqual(term.cursor_style, 0)

    def test_reverse_video_and_newline_mode(self):
        term = feed(terminal(), "\x1b[?5h\x1b[20hab\ncd")
        self.assertTrue(term.reverse_video)
        self.assertEqual(term.screen_text()[:2], ["ab", "cd"])

    def test_mode_reports(self):
        term = feed(terminal(), "\x1b[?2004h\x1b[?2004$p\x1b[?1$p\x1b[?4242$p\x1b[4$p")
        self.assertEqual(term.replies, [b"\x1b[?2004;1$y", b"\x1b[?1;2$y", b"\x1b[?4242;0$y", b"\x1b[4;2$y"])


class Replies(unittest.TestCase):
    def test_device_attributes_and_status(self):
        term = feed(terminal(), "\x1b[c\x1b[>c\x1b[5n\x1b[3;4H\x1b[6n\x1b[?6n\x1bZ\x1b[>q")
        self.assertEqual(term.replies, [b"\x1b[?62;22c", b"\x1b[>1;277;0c", b"\x1b[0n", b"\x1b[3;4R", b"\x1b[?3;4R",
                                        b"\x1b[?62;22c", b"\x1bP>|boswas-terminal(1.0)\x1b\\"])

    def test_window_size_reports(self):
        term = terminal()
        term.cell_width, term.cell_height = 9, 18
        feed(term, "\x1b[18t\x1b[14t\x1b[16t")
        self.assertEqual(term.replies, [b"\x1b[8;5;20t", b"\x1b[4;90;180t", b"\x1b[6;18;9t"])

    def test_title_is_never_reported(self):
        term = feed(terminal(), "\x1b]0;secret\x07\x1b[21t\x1b[20t")
        self.assertEqual(term.replies, [])

    def test_requests_for_settings(self):
        term = feed(terminal(), "\x1b[1;31m\x1bP$qm\x1b\\\x1b[2;4r\x1bP$qr\x1b\\\x1b[4 q\x1bP$q q\x1b\\\x1bP$qx\x1b\\")
        self.assertEqual(term.replies, [b"\x1bP1$r0;1;31m\x1b\\", b"\x1bP1$r2;4r\x1b\\", b"\x1bP1$r4 q\x1b\\",
                                        b"\x1bP0$r\x1b\\"])

    def test_termcap_requests(self):
        term = feed(terminal(), "\x1bP+q" + b"TN".hex() + ";" + b"zz".hex() + "\x1b\\")
        self.assertEqual(term.replies, [b"\x1bP1+r544e=" + b"xterm-256color".hex().encode() + b"\x1b\\",
                                        b"\x1bP0+r\x1b\\"])

    def test_colour_queries_use_the_provider(self):
        term = terminal()
        term.colour_provider = lambda key: {"fg": (255, 255, 255), "bg": (0, 0, 0), 1: (128, 0, 0)}.get(key)
        feed(term, "\x1b]10;?\x07\x1b]11;?\x1b\\\x1b]4;1;?\x07")
        self.assertEqual(term.replies, [b"\x1b]10;rgb:ffff/ffff/ffff\x07", b"\x1b]11;rgb:0000/0000/0000\x1b\\",
                                        b"\x1b]4;1;rgb:8080/0000/0000\x07"])

    def test_programs_can_set_colours(self):
        term = terminal()
        changes = []
        term.on_colours = lambda: changes.append(1)
        feed(term, "\x1b]4;1;rgb:ff/00/80\x07\x1b]11;#102030\x07\x1b]12;#fff\x07")
        self.assertEqual(term.colours, {1: (255, 0, 128), "bg": (16, 32, 48), "cursor": (255, 255, 255)})
        feed(term, "\x1b]104\x07\x1b]111\x07")
        self.assertEqual(term.colours, {"cursor": (255, 255, 255)})
        self.assertEqual(len(changes), 5)

    def test_colour_scheme_reports(self):
        term = feed(terminal(), "\x1b[?996n")
        self.assertEqual(term.replies, [b"\x1b[?997;1n"])
        term.report_scheme(False)
        self.assertEqual(len(term.replies), 1)
        feed(term, "\x1b[?2031h")
        term.report_scheme(True)
        self.assertEqual(term.replies[-1], b"\x1b[?997;1n")


class OperatingSystemCommands(unittest.TestCase):
    def test_title(self):
        term = terminal()
        titles = []
        term.on_title = titles.append
        feed(term, "\x1b]0;one\x07\x1b]2;two\x1b\\\x1b]1;icon\x07")
        self.assertEqual(titles, ["one", "two"])
        feed(term, "\x1b]2;a\x00b\rc\x07")
        self.assertEqual(term.title, "abc")
        feed(term, "\x1b]2;" + "x" * 5000 + "\x07")
        self.assertEqual(len(term.title), emulator.MAX_TITLE)

    def test_title_stack(self):
        term = feed(terminal(), "\x1b]2;first\x07\x1b[22t\x1b]2;second\x07\x1b[23t")
        self.assertEqual(term.title, "first")
        feed(term, "\x1b[23t" + "\x1b[22t" * 100)
        self.assertLessEqual(len(term._title_stack), emulator.MAX_TITLE_STACK)

    def test_current_directory(self):
        term = terminal()
        term.hostname = "box"
        seen = []
        term.on_cwd = seen.append
        feed(term, "\x1b]7;file://box/home/mira/My%20Files\x07")
        self.assertEqual(term.cwd, "/home/mira/My Files")
        feed(term, "\x1b]7;file:///tmp\x1b\\")
        self.assertEqual(term.cwd, "/tmp")
        feed(term, "\x1b]7;file://elsewhere/etc\x07\x1b]7;http://box/x\x07\x1b]7;file://box/a%00b\x07\x1b]7;relative\x07")
        self.assertEqual(seen, ["/home/mira/My Files", "/tmp"])

    def test_hyperlinks(self):
        term = feed(terminal(cols=30), "a\x1b]8;id=x;https://example.org/\x1b\\link\x1b]8;;\x1b\\b")
        self.assertEqual(term.link_at(0, 0), "")
        self.assertEqual(term.link_at(0, 1), "https://example.org/")
        self.assertEqual(term.link_at(0, 4), "https://example.org/")
        self.assertEqual(term.link_at(0, 5), "")
        feed(term, "\x1b]8;;https://example.org/" + "x" * 3000 + "\x07c")
        self.assertEqual(term.link_at(0, 6), "")

    def test_hyperlinks_survive_sgr_reset_and_are_capped(self):
        term = feed(terminal(cols=30), "\x1b]8;;https://a.example/\x07x\x1b[0my\x1b]8;;\x07")
        self.assertEqual(term.link_at(0, 1), "https://a.example/")
        for number in range(emulator.MAX_LINKS + 50):
            feed(term, f"\x1b]8;;https://example.org/{number}\x07")
        self.assertLessEqual(len(term.links), emulator.MAX_LINKS)

    def test_clipboard_write_needs_permission(self):
        term = terminal()
        written, denied = [], []
        term.on_clipboard = written.append
        term.on_clipboard_denied = lambda: denied.append(1)
        payload = base64.b64encode("copied ✓".encode()).decode()
        feed(term, f"\x1b]52;c;{payload}\x07")
        self.assertEqual((written, denied), ([], [1]))
        term.allow_clipboard_write = True
        feed(term, f"\x1b]52;c;{payload}\x07\x1b]52;;{payload}\x1b\\")
        self.assertEqual(written, ["copied ✓", "copied ✓"])

    def test_clipboard_is_never_read(self):
        term = terminal()
        term.allow_clipboard_write = True
        written = []
        term.on_clipboard = written.append
        feed(term, "\x1b]52;c;?\x07\x1b]52;p;?\x07\x1b]52;c;not base64!\x07")
        self.assertEqual(term.replies, [])
        self.assertEqual(written, [])

    def test_clipboard_size_is_capped(self):
        term = terminal()
        term.allow_clipboard_write = True
        written = []
        term.on_clipboard = written.append
        feed(term, "\x1b]52;c;" + base64.b64encode(b"x" * (emulator.MAX_CLIPBOARD * 2)).decode() + "\x07")
        self.assertEqual(written, [])


class Hostile(unittest.TestCase):
    def test_unknown_sequences_print_nothing(self):
        term = feed(terminal(), "a\x1b[?9999hb\x1b[99zc\x1b[1;2;3 Zd\x1b]9999;x\x07e\x1bP1;2|data\x1b\\f\x1b_apc\x1b\\g"
                                "\x1b^pm\x1b\\h\x1bXsos\x1b\\i\x1b[<1;2;3Mj\x1b%Gk\x1b#3l\x1b[=1cm\x1b[>4;2mn")
        self.assertEqual(term.screen_text()[0], "abcdefghijklmn")

    def test_control_characters_inside_a_sequence_act_at_once(self):
        term = feed(terminal(), "ab\x1b[\r2Cx")
        self.assertEqual(term.screen_text()[0], "abx")

    def test_cancel_and_restart(self):
        term = feed(terminal(), "a\x1b[12\x18b\x1b[1\x1b[2Cc\x1b]0;title\x18d")
        self.assertEqual(term.screen_text()[0], "ab  cd")
        self.assertEqual(term.title, "")

    def test_sequence_split_at_every_position(self):
        data = "x\x1b[1;31mred\x1b[0m\x1b]2;title\x1b\\\x1b[2;3Hy\x1b(0q\x1b(B\x1bP$qm\x1b\\z".encode()
        whole = feed(terminal(), data)
        for cut in range(1, len(data)):
            term = terminal()
            term.feed(data[:cut])
            term.feed(data[cut:])
            self.assertEqual(term.screen_text(), whole.screen_text(), cut)
            self.assertEqual(term.title, "title", cut)
            self.assertEqual(term.replies, whole.replies, cut)

    def test_huge_parameters(self):
        term = feed(terminal(), "\x1b[99999999999999999999;99999999999999999999H\x1b[9999999999Lx\x1b[999999999@"
                                "\x1b[99999999999b\x1b[9999999S\x1b[999999999999T\x1b[99999X\x1b[99999P\x1b[99999M")
        self.assertEqual(len(term.lines), 5)
        self.assertTrue(all(len(line.chars) == 20 for line in term.lines))

    def test_too_many_parameters(self):
        term = feed(terminal(), "\x1b[" + ";".join(["1"] * 10000) + "mA\x1b[" + "1;" * 400 + "HB")
        self.assertIn("A", term.screen_text()[0])

    def test_parameter_part_is_capped(self):
        term = feed(terminal(), "\x1b[" + "9" * 100000 + "CX")
        self.assertEqual(term.screen_text()[0], "X")

    def test_unterminated_strings_are_bounded(self):
        term = terminal()
        feed(term, "\x1b]2;" + "A" * (emulator.MAX_STRING * 2))
        self.assertLessEqual(term._string_length, emulator.MAX_STRING)
        feed(term, "\x07after")
        self.assertEqual(term.screen_text()[0], "after")
        self.assertEqual(len(term.title), emulator.MAX_TITLE)
        feed(term, "\x1bP" + "B" * (emulator.MAX_STRING * 2) + "\x1b\\ok")
        self.assertIn("ok", term.screen_text()[0])

    def test_attribute_and_cache_growth_is_bounded(self):
        term = terminal()
        rng = random.Random(7)
        for _ in range(emulator.MAX_ATTRS * 2 + 10):
            feed(term, f"\x1b[38;2;{rng.randrange(256)};{rng.randrange(256)};{rng.randrange(256)}mx")
        self.assertLessEqual(len(term._attr_cache), emulator.MAX_ATTRS)
        self.assertLessEqual(len(term._sgr_cache), emulator.MAX_ATTRS)

    def test_c1_controls_are_not_interpreted(self):
        term = feed(terminal(), "a\u009b31mb\u0090x\u009cc\u0085d")
        self.assertEqual(term.screen_text()[0], "a31mbxcd")
        self.assertEqual(attr_at(term, 0, 4), DEFAULT_ATTR)

    def test_random_bytes_never_raise(self):
        rng = random.Random(20261116)
        pieces = [b"\x1b[", b"\x1b]", b"\x1bP", b"\x1b\\", b"\x07", b";", b"?", b"\x1b", b"\r\n", b"\x1b[?1049h",
                  b"\x1b[?1049l", b"\x1b[38;2;", b"\x1b[48;5;", "日本".encode(), "é".encode(), b"\x1b[r",
                  b"\x1b[?6h", b"\x1b[4h", b"\x1b(0", b"\x1b[?7l", b"\x1b[?7h", "👍".encode(), b"\x1b[1;3r",
                  b"\x1b[L", b"\x1b[M", b"\x1b[@", b"\x1b[P", b"\x1b[X", b"\x1bM", b"\x1bD", b"\x1b7", b"\x1b8", b"\x08",
                  b"\t", b"\x1b[b", b"\x1b#8", b"\x1bc", b"\x1b[!p", b"\x1b[3J", b"\x1b]8;;http://x\x07", b"m", b"H"]
        for size in ((7, 3), (20, 5), (2, 1), (80, 24)):
            term = terminal(cols=size[0], rows=size[1], scrollback=50)
            for step in range(1500):
                kind = rng.random()
                if kind < 0.4:
                    data = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40)))
                elif kind < 0.8:
                    data = b"".join(rng.choice(pieces) + str(rng.randrange(0, 300)).encode() * rng.randrange(0, 2)
                                    + bytes([rng.randrange(0x20, 0x7f)]) for _ in range(rng.randrange(1, 12)))
                else:
                    data = bytes(rng.choice(b"\x1b[];?0123456789:mHJKABCDrhl \r\n\x07\\") for _ in range(30))
                term.feed(data)
                if step % 97 == 0:
                    term.resize(rng.randrange(1, 60), rng.randrange(1, 30))
                if step % 50 == 0:
                    self.check(term)
            self.check(term)

    def check(self, term):
        self.assertEqual(len(term.lines), term.rows)
        self.assertTrue(0 <= term.x < term.cols and 0 <= term.y < term.rows, (term.x, term.y, term.cols, term.rows))
        self.assertTrue(0 <= term.top <= term.bottom < term.rows)
        for line in term.lines:
            self.assertEqual(len(line.chars), term.cols)
            self.assertEqual(len(line.attrs), term.cols)
        for index in range(term.total_lines()):
            cells, runs, _wrapped = term.display_line(index)
            if runs is not None:
                self.assertEqual(sum(runs[at] for at in range(0, len(runs), 2)), len(cells))
            self.assertLessEqual(len(cells), max(term.cols, len(cells) if index < len(term.history) else 0))


class Resize(unittest.TestCase):
    def test_narrower_wraps_and_wider_joins_again(self):
        term = feed(terminal(cols=20, rows=5), "the quick brown fox\r\njumps")
        term.resize(10, 5)
        self.assertEqual(term.screen_text()[:3], ["the quick", "brown fox", "jumps"])
        self.assertTrue(term.lines[0].wrapped)
        self.assertEqual((term.x, term.y), (5, 2))
        term.resize(20, 5)
        self.assertEqual(term.screen_text()[:2], ["the quick brown fox", "jumps"])
        self.assertEqual((term.x, term.y), (5, 1))

    def test_content_goes_to_the_scrollback_and_comes_back(self):
        term = feed(terminal(cols=10, rows=4, scrollback=50), "".join(f"row{i}\r\n" for i in range(4)))
        before = [term.line_text(i) for i in range(term.total_lines())]
        term.resize(10, 2)
        self.assertEqual([term.line_text(i) for i in range(term.total_lines())][:5], before[:5])
        self.assertEqual(term.y, 1)
        term.resize(10, 6)
        self.assertEqual([term.line_text(i) for i in range(term.total_lines())][:4], ["row0", "row1", "row2", "row3"])

    def test_long_line_through_scrollback(self):
        term = terminal(cols=10, rows=3, scrollback=50)
        text = "".join(chr(ord("a") + i % 26) for i in range(47))
        feed(term, text + "\r\nend")
        term.resize(7, 3)
        term.resize(25, 3)
        joined = [term.line_text(i) for i in range(term.total_lines()) if term.line_text(i)]
        self.assertEqual(joined, [text[:25], text[25:], "end"])
        term.resize(60, 3)
        joined = [term.line_text(i) for i in range(term.total_lines()) if term.line_text(i)]
        self.assertEqual(joined, [text, "end"])

    def test_wide_characters_are_not_cut(self):
        term = feed(terminal(cols=10, rows=3), "日本語日本語")
        term.resize(5, 3)
        self.assertEqual(term.screen_text()[:3], ["日本", "語日", "本語"])
        for line in term.lines:
            self.assertEqual(len(line.chars), 5)
        term.resize(12, 3)
        self.assertEqual("".join(term.screen_text()).replace(" ", ""), "日本語日本語")

    def test_attributes_survive(self):
        term = feed(terminal(cols=10, rows=3), "ab\x1b[31mcdefghijkl\x1b[0mmn")
        term.resize(4, 5)
        self.assertEqual(attr_at(term, 0, 1), DEFAULT_ATTR)
        self.assertEqual(attr_at(term, 0, 2)[0], PALETTE | 1)
        self.assertEqual(attr_at(term, 2, 3)[0], PALETTE | 1)
        self.assertEqual(attr_at(term, 3, 0), DEFAULT_ATTR)

    def test_alternate_screen_is_cut_and_main_screen_reflowed(self):
        term = feed(terminal(cols=20, rows=4), "a long line of words\x1b[?1049h\x1b[4;18Hxyz")
        term.resize(10, 2)
        self.assertEqual(len(term.lines), 2)
        self.assertTrue(all(len(line.chars) == 10 for line in term.lines))
        feed(term, "\x1b[?1049l")
        text = [term.line_text(i) for i in range(term.total_lines())]
        self.assertEqual("".join(text), "a long line of words")
        self.assertTrue(0 <= term.x < 10 and 0 <= term.y < 2)

    def test_sizes_are_clamped(self):
        term = terminal()
        term.resize(0, 0)
        self.assertEqual((term.cols, term.rows), (emulator.MIN_COLS, emulator.MIN_ROWS))
        term.resize(10 ** 9, 10 ** 9)
        self.assertEqual((term.cols, term.rows), (emulator.MAX_COLS, emulator.MAX_ROWS))

    def test_reflow_function_keeps_marks(self):
        entries = [("abcdefghij", None, True), ("klm", None, False), ("x", None, False)]
        rows, marks = emulator.reflow(entries, 5, [(1, 2), (2, 0), (0, 0)])
        self.assertEqual([row[0] for row in rows], ["abcde", "fghij", "klm", "x"])
        self.assertEqual(marks, [(2, 2), (3, 0), (0, 0)])
        self.assertEqual([row[2] for row in rows], [True, True, False, False])


if __name__ == "__main__":
    unittest.main()
