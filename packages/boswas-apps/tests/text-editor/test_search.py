"""Find and replace: positions in UTF-16 units, the options, regular expressions, hostile expressions."""

from __future__ import annotations

import time
import unittest

from boswas_apps.text_editor.search import MAX_COUNT, Options, Query, utf16_length

TEXT = "The cat sat.\nA Cat, a catalogue, a bobcat.\nconcat\n"


def spans(query: Query, text: str) -> list[str]:
    found = []
    position = 0
    while True:
        match = query.find_next(text, position)
        if match is None or match[2]:
            return found
        found.append(text.encode("utf-16-le")[match[0] * 2:match[1] * 2].decode("utf-16-le"))
        position = match[1]


class FindTest(unittest.TestCase):
    def test_plain(self):
        query = Query("cat")
        self.assertEqual(query.find_next(TEXT, 0), (4, 7, False))
        self.assertEqual(spans(query, TEXT), ["cat", "Cat", "cat", "cat", "cat"])
        self.assertEqual(query.count(TEXT), (5, 0, False))
        self.assertEqual(query.count(TEXT, 16), (5, 2, False))

    def test_match_case_and_whole_word(self):
        self.assertEqual(spans(Query("cat", Options(match_case=True)), TEXT), ["cat", "cat", "cat", "cat"])
        self.assertEqual(spans(Query("cat", Options(whole_word=True)), TEXT), ["cat", "Cat"])
        self.assertEqual(spans(Query("Cat", Options(whole_word=True, match_case=True)), TEXT), ["Cat"])
        self.assertEqual(spans(Query("a", Options(whole_word=True)), "a b_a a1 äa a"), ["a", "a"])

    def test_plain_text_is_literal(self):
        text = "price (a+b)* is $5.00 [x] ^ end \\n"
        for needle in ("(a+b)*", "$5.00", "[x]", "^", ".", "\\n"):
            with self.subTest(needle=needle):
                found = Query(needle).find_next(text, 0)
                self.assertIsNotNone(found)
                self.assertEqual(text[found[0]:found[1]], needle)

    def test_wrap_around(self):
        query = Query("cat")
        self.assertEqual(query.find_next(TEXT, 48), (4, 7, True))
        self.assertIsNone(Query("cat", Options(wrap_around=False)).find_next(TEXT, 48))
        self.assertIsNone(Query("dog").find_next(TEXT, 10))
        self.assertEqual(query.find_previous(TEXT, 4), (46, 49, True))
        self.assertIsNone(Query("cat", Options(wrap_around=False)).find_previous(TEXT, 4))

    def test_previous(self):
        query = Query("cat")
        self.assertEqual(query.find_previous(TEXT, len(TEXT)), (46, 49, False))
        self.assertEqual(query.find_previous(TEXT, 45), (38, 41, False))
        self.assertEqual(query.find_previous(TEXT, 7), (4, 7, False))
        self.assertEqual(query.find_previous(TEXT, 6), (46, 49, True))

    def test_previous_far_back(self):
        text = "needle" + " hay" * 200000
        query = Query("needle", Options(wrap_around=False))
        started = time.monotonic()
        self.assertEqual(query.find_previous(text, len(text)), (0, 6, False))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_positions_are_utf16_units(self):
        text = "\U0001f600 smile \U0001f600\U0001f600 end smile"
        query = Query("smile")
        self.assertEqual(query.find_next(text, 0), (3, 8, False))          # the emoji takes two units
        self.assertEqual(query.find_next(text, 8), (18, 23, False))
        self.assertEqual(query.find_previous(text, 18), (3, 8, False))
        self.assertEqual(utf16_length(text), 23)
        self.assertEqual(Query("\U0001f600").count(text), (3, 0, False))
        replaced, count = Query("smile").replace_all(text, "\U0001f642")
        self.assertEqual((replaced, count), ("\U0001f600 \U0001f642 \U0001f600\U0001f600 end \U0001f642", 2))

    def test_unicode_case(self):
        self.assertEqual(spans(Query("straße"), "Straße STRASSE straße"), ["Straße", "straße"])
        self.assertEqual(spans(Query("é"), "Été"), ["É", "é"])

    def test_empty_and_long_queries(self):
        self.assertFalse(Query("").valid)
        self.assertIsNone(Query("").find_next(TEXT, 0))
        self.assertEqual(Query("").count(TEXT), (0, 0, False))
        self.assertEqual(Query("").replace_all(TEXT, "x"), (TEXT, 0))
        self.assertFalse(Query("x" * 20000).valid)

    def test_count_is_capped(self):
        text = "a" * (MAX_COUNT + 500)
        self.assertEqual(Query("a").count(text), (MAX_COUNT, 0, True))
        self.assertEqual(Query("a").count(text, 0, limit=10), (10, 0, True))


class RegexTest(unittest.TestCase):
    def options(self, **more) -> Options:
        return Options(regex=True, **more)

    def test_expressions(self):
        self.assertEqual(spans(Query(r"\bc\w+", self.options()), TEXT), ["cat", "Cat", "catalogue", "concat"])
        self.assertEqual(spans(Query(r"^\w+", self.options()), TEXT), ["The", "A", "concat"])
        self.assertEqual(spans(Query(r"\.$", self.options()), TEXT), [".", "."])
        self.assertEqual(spans(Query(r"c.t", self.options(match_case=True)), "cat c\nt cut"), ["cat", "cut"])
        self.assertEqual(spans(Query(r"\d+", self.options()), "a1 b22 c333"), ["1", "22", "333"])

    def test_malformed(self):
        for pattern in ("(", "[a-", "a{2,1}", "(?<name", "\\"):
            with self.subTest(pattern=pattern):
                query = Query(pattern, self.options())
                self.assertFalse(query.valid)
                self.assertTrue(query.error.startswith("Not a regular expression"))
                self.assertIsNone(query.find_next(TEXT, 0))
                self.assertEqual(query.replace_all(TEXT, "x"), (TEXT, 0))

    def test_empty_matches_are_not_finds(self):
        query = Query("x*", self.options())
        self.assertEqual(query.find_next("aaxxa", 0), (2, 4, False))
        self.assertEqual(query.count("aaxxa"), (1, 0, False))
        self.assertIsNone(Query("^", self.options()).find_next("a\nb", 0))
        self.assertEqual(Query("x*", self.options()).replace_all("aaxxa", "-"), ("aa-a", 1))

    def test_an_expression_that_would_take_forever_gives_up(self):
        started = time.monotonic()
        query = Query(r"(a+)+$", self.options())
        self.assertIsNone(query.find_next("a" * 5000 + "!", 0))
        self.assertEqual(query.count("a" * 5000 + "!")[0], 0)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_replacement_groups(self):
        query = Query(r"(\w+)@(\w+)", self.options())
        self.assertEqual(query.replace_all("a@b c@d", r"$2 at \1"), ("b at a d at c", 2))
        self.assertEqual(query.replace_all("a@b", r"[\0] [$0] \9 \\1 \$1 \t|\n|"), ("[a@b] [a@b]  \\1 $1 \t|\n|", 1))
        plain = Query("a@b")
        self.assertEqual(plain.replace_all("a@b", r"$1 \1 \n"), (r"$1 \1 \n", 1))     # literal without regex

    def test_replace_one(self):
        query = Query(r"c(a)t", self.options())
        self.assertEqual(query.replace_one(TEXT, 4, 7, r"<\1>"), "<a>")
        self.assertIsNone(query.replace_one(TEXT, 4, 6, "x"))             # the selection is not a match
        self.assertIsNone(query.replace_one(TEXT, 0, 3, "x"))
        self.assertEqual(Query("cat").replace_one(TEXT, 4, 7, "dog"), "dog")

    def test_replace_all_plain(self):
        self.assertEqual(Query("cat", Options(match_case=True)).replace_all(TEXT, "dog"),
                         ("The dog sat.\nA Cat, a dogalogue, a bobdog.\ncondog\n", 4))
        self.assertEqual(Query("zebra").replace_all(TEXT, "x"), (TEXT, 0))
        self.assertEqual(Query("a").replace_all("aaa", ""), ("", 3))

    def test_large_text(self):
        text = "The quick brown fox jumps over the lazy dog 0123456789\n" * 200000      # 11 MB
        started = time.monotonic()
        query = Query("lazy")
        self.assertEqual(query.find_next(text, len(text) - 100)[2], False)
        self.assertEqual(query.count(text)[0], MAX_COUNT)
        replaced, count = Query("quick").replace_all(text, "slow")
        self.assertEqual(count, 200000)
        self.assertEqual(len(replaced), len(text) - 200000)
        self.assertLess(time.monotonic() - started, 20.0)


if __name__ == "__main__":
    unittest.main()
