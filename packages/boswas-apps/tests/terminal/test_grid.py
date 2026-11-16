"""Selections, words, addresses and search in the terminal's text."""

import unittest

from boswas_apps.terminal import grid
from boswas_apps.terminal.emulator import Terminal
from boswas_apps.terminal.grid import Selection


def terminal(text, cols=20, rows=5, scrollback=100):
    term = Terminal(cols, rows, scrollback)
    term.feed(text.encode())
    return term


class SelectionText(unittest.TestCase):
    def test_within_a_line(self):
        term = terminal("hello world")
        self.assertEqual(grid.selection_text(term, Selection((0, 6), (0, 10))), "world")
        self.assertEqual(grid.selection_text(term, Selection((0, 0), (0, 0))), "h")

    def test_trailing_blanks_are_trimmed(self):
        term = terminal("abc\r\ndef")
        self.assertEqual(grid.selection_text(term, Selection((0, 0), (1, 19))), "abc\ndef")
        self.assertEqual(grid.selection_text(term, Selection((0, 1), (0, 19))), "bc")

    def test_wrapped_lines_join(self):
        term = terminal("x" * 20 + "y" * 5 + "\r\nnext", cols=20)
        self.assertEqual(grid.selection_text(term, Selection((0, 0), (2, 3))), "x" * 20 + "y" * 5 + "\nnext")
        self.assertEqual(grid.selection_text(term, Selection((0, 18), (1, 1))), "xxyy")

    def test_wrapped_line_keeps_inner_blanks(self):
        term = terminal("a" * 18 + "  " + "b" * 3, cols=20)
        self.assertEqual(grid.selection_text(term, Selection((0, 0), (1, 19))), "a" * 18 + "  " + "bbb")

    def test_scrollback_and_screen(self):
        term = terminal("".join(f"line{i}\r\n" for i in range(8)), rows=3)
        self.assertEqual(term.line_text(0), "line0")
        self.assertEqual(grid.selection_text(term, Selection((0, 0), (2, 19))), "line0\nline1\nline2")
        last = term.total_lines() - 1
        self.assertEqual(grid.selection_text(term, Selection((last - 2, 4), (last - 1, 3))), "6\nline")

    def test_block(self):
        term = terminal("abcdef\r\nghijkl\r\nmn")
        self.assertEqual(grid.selection_text(term, Selection((0, 1), (2, 3), True)), "bcd\nhij\nn")
        self.assertEqual(grid.selection_text(term, Selection((0, 3), (2, 1), True)), "bcd\nhij\nn")

    def test_wide_and_combining(self):
        term = terminal("a日本éz")
        self.assertEqual(grid.selection_text(term, Selection((0, 0), (0, 19))), "a日本éz")
        self.assertEqual(grid.selection_text(term, Selection((0, 2), (0, 3))), "日本")   # from the second half
        self.assertEqual(grid.selection_text(term, Selection((0, 1), (0, 1))), "日")

    def test_spans(self):
        selection = Selection((2, 5), (4, 3))
        self.assertIsNone(selection.span(1, 80))
        self.assertEqual(selection.span(2, 80), (5, 80))
        self.assertEqual(selection.span(3, 80), (0, 80))
        self.assertEqual(selection.span(4, 80), (0, 4))
        self.assertEqual(Selection((2, 5), (4, 3), True).span(3, 80), (3, 6))
        self.assertTrue(selection.contains(3, 7))
        self.assertFalse(selection.contains(4, 4))
        self.assertEqual(Selection.between((4, 3), (2, 5)), selection)

    def test_out_of_range_is_harmless(self):
        term = terminal("abc")
        self.assertEqual(grid.selection_text(term, Selection((-5, 0), (999, 500))), "abc\n\n\n\n")
        self.assertEqual(grid.selection_text(term, Selection((0, 50), (0, 90))), "")


class Words(unittest.TestCase):
    def test_word_range(self):
        text = "cp ~/notes/a-b.txt /tmp;ls"
        self.assertEqual(text[slice(*grid.word_range(text, 0))], "cp")
        self.assertEqual(text[slice(*grid.word_range(text, 5))], "~/notes/a-b.txt")
        self.assertEqual(text[slice(*grid.word_range(text, 2))], " ")
        self.assertEqual(text[slice(*grid.word_range(text, 23))], ";")
        self.assertEqual(grid.word_range("", 3), (0, 0))

    def test_word_at(self):
        term = terminal("see ~/a.txt now")
        self.assertEqual(grid.word_at(term, 0, 6), ((0, 4), (0, 10)))
        self.assertEqual(grid.word_at(term, 0, 0), ((0, 0), (0, 2)))

    def test_word_across_a_wrap(self):
        term = terminal("x" * 15 + " " + "w" * 10, cols=20)
        self.assertEqual(grid.word_at(term, 1, 2), ((0, 16), (1, 5)))
        self.assertEqual(grid.word_at(term, 0, 18), ((0, 16), (1, 5)))

    def test_word_with_wide_characters(self):
        term = terminal("ab 日本語 cd")
        self.assertEqual(grid.word_at(term, 0, 5), ((0, 3), (0, 8)))

    def test_blank_area(self):
        term = terminal("abc")
        start, end = grid.word_at(term, 0, 10)
        self.assertEqual(grid.selection_text(term, Selection(start, end)), "")

    def test_line_range(self):
        term = terminal("x" * 45 + "\r\nshort", cols=20)
        self.assertEqual(grid.line_range(term, 1), (0, 2))
        self.assertEqual(grid.line_range(term, 3), (3, 3))


class Addresses(unittest.TestCase):
    def urls(self, text):
        return [url for _start, _end, url in grid.find_urls(text)]

    def test_plain(self):
        self.assertEqual(self.urls("see https://example.org/a?b=c#d and http://x.test"),
                         ["https://example.org/a?b=c#d", "http://x.test"])

    def test_trailing_punctuation(self):
        self.assertEqual(self.urls("Go to https://example.org/path. Then"), ["https://example.org/path"])
        self.assertEqual(self.urls("(https://example.org/a)"), ["https://example.org/a"])
        self.assertEqual(self.urls("https://en.wikipedia.org/wiki/Rust_(language),"),
                         ["https://en.wikipedia.org/wiki/Rust_(language)"])
        self.assertEqual(self.urls("<https://example.org>"), ["https://example.org"])
        self.assertEqual(self.urls("'https://example.org/x'!"), ["https://example.org/x"])

    def test_other_schemes(self):
        self.assertEqual(self.urls("mailto:mira@example.org file:///etc/hosts ftp://ftp.example.org/x"),
                         ["mailto:mira@example.org", "file:///etc/hosts", "ftp://ftp.example.org/x"])

    def test_not_addresses(self):
        self.assertEqual(self.urls("http:// https://. nothing://x javascript:alert(1) httpx"), [])

    def test_positions(self):
        self.assertEqual(grid.find_urls("ab https://x.test."), [(3, 17, "https://x.test")])

    def test_url_at_cell(self):
        term = terminal("go https://example.org/x now")
        self.assertIsNone(grid.url_at(term, 0, 1))
        self.assertEqual(grid.url_at(term, 0, 5), ("https://example.org/x", [(0, 3, 20), (1, 0, 4)]))
        self.assertEqual(grid.url_at(term, 1, 2)[0], "https://example.org/x")
        self.assertIsNone(grid.url_at(term, 1, 5))
        self.assertIsNone(grid.url_at(term, 99, 0))

    def test_box_drawing_ends_an_address(self):
        self.assertEqual(self.urls("│https://example.org/a│"), ["https://example.org/a"])


class Search(unittest.TestCase):
    def find(self, term, needle, match_case=False):
        found = []
        for matches in grid.search(term.iter_lines(), grid.search_pattern(needle, match_case)):
            found.extend(matches)
        return found

    def test_simple(self):
        term = terminal("one two\r\nTwo three\r\ntwo")
        self.assertEqual(self.find(term, "two"), [[(0, 4, 7)], [(1, 0, 3)], [(2, 0, 3)]])
        self.assertEqual(self.find(term, "two", True), [[(0, 4, 7)], [(2, 0, 3)]])
        self.assertEqual(self.find(term, "absent"), [])

    def test_the_needle_is_literal(self):
        term = terminal("a.c abc a*c")
        self.assertEqual(self.find(term, "a.c"), [[(0, 0, 3)]])
        self.assertEqual(self.find(term, "a*c"), [[(0, 8, 11)]])

    def test_across_a_wrap(self):
        term = terminal("x" * 18 + "needle", cols=20)
        self.assertEqual(self.find(term, "needle"), [[(0, 18, 20), (1, 0, 4)]])

    def test_scrollback_lines_have_their_index(self):
        term = terminal("".join(f"row{i}\r\n" for i in range(10)), rows=3)
        found = self.find(term, "row7")
        self.assertEqual(found, [[(7, 0, 4)]])
        self.assertEqual(term.line_text(7), "row7")

    def test_wide_characters_give_cell_columns(self):
        term = terminal("日本 abc 語")
        self.assertEqual(self.find(term, "abc"), [[(0, 5, 8)]])
        self.assertEqual(self.find(term, "本"), [[(0, 2, 4)]])
        self.assertEqual(self.find(term, "語"), [[(0, 9, 11)]])

    def test_first_index_offsets_the_lines(self):
        term = terminal("abc")
        found = list(grid.search(term.iter_lines(), grid.search_pattern("b", False), 1000))
        self.assertEqual(found[0], [[(1000, 1, 2)]])


if __name__ == "__main__":
    unittest.main()
