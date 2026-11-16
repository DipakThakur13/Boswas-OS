"""Names, natural order, sizes."""

import locale
import random
import unittest

import support  # noqa: F401

from boswas_apps.files import text


class NaturalOrderTest(unittest.TestCase):
    def ordered(self, names):
        return sorted(names, key=text.natural_key)

    def test_numbers_compare_by_value(self):
        self.assertEqual(self.ordered(["file10", "file2", "file1", "file20", "file3"]),
                         ["file1", "file2", "file3", "file10", "file20"])

    def test_numbers_inside_and_at_the_start(self):
        self.assertEqual(self.ordered(["x10y2", "x2y10", "x2y9", "10", "9", "a"]),
                         ["9", "10", "a", "x2y9", "x2y10", "x10y2"])

    def test_case_does_not_matter_but_order_is_total(self):
        names = ["b", "A", "a", "B", "c"]
        ordered = self.ordered(names)
        self.assertEqual([name.lower() for name in ordered], ["a", "a", "b", "b", "c"])
        self.assertEqual(self.ordered(list(reversed(names))), ordered)

    def test_leading_zeros(self):
        self.assertEqual(self.ordered(["a01", "a1", "a001", "a2"]), ["a1", "a01", "a001", "a2"])

    def test_huge_numbers_do_not_overflow(self):
        big = "9" * 6000
        bigger = "1" + "0" * 6000
        self.assertEqual(self.ordered([f"n{bigger}", f"n{big}", "n5"]), ["n5", f"n{big}", f"n{bigger}"])

    def test_prefix_sorts_first(self):
        self.assertEqual(self.ordered(["file1b", "file", "file1"]), ["file", "file1", "file1b"])

    def test_hostile_names(self):
        names = ["\n", "-rf", "..", "a\0b", "\udcff\udcfe", "", " ", "é", "E", "名前", "x" * 5000, "🙂2", "🙂10"]
        ordered = self.ordered(names)
        self.assertEqual(sorted(ordered), sorted(names))
        self.assertLess(ordered.index("🙂2"), ordered.index("🙂10"))

    def test_shuffles_give_the_same_order(self):
        names = [f"item{n}{s}" for n in range(60) for s in ("", "a", ".txt")]
        expected = self.ordered(names)
        rng = random.Random(7)
        for _ in range(5):
            rng.shuffle(names)
            self.assertEqual(self.ordered(names), expected)

    def test_uses_the_collation_of_the_locale(self):
        calls = []
        original = locale.strxfrm
        locale.strxfrm = lambda value: (calls.append(value), original(value))[1]
        try:
            text.natural_key("Zebra7")
        finally:
            locale.strxfrm = original
        self.assertEqual(calls, ["zebra"])


class NamesTest(unittest.TestCase):
    def test_split_extension(self):
        cases = {"report.pdf": ("report", ".pdf"), "backup.tar.gz": ("backup", ".tar.gz"), ".bashrc": (".bashrc", ""),
                 "README": ("README", ""), "a.b.c": ("a.b", ".c"), "trailing.": ("trailing.", ""),
                 ".tar.gz": (".tar", ".gz"), "x.TAR.XZ": ("x", ".TAR.XZ"), "": ("", "")}
        for name, expected in cases.items():
            self.assertEqual(text.split_extension(name), expected, name)

    def test_validate_name(self):
        self.assertIsNone(text.validate_name("ok.txt"))
        self.assertIsNone(text.validate_name("-rf"))
        self.assertIsNone(text.validate_name("line\nbreak"))
        self.assertIsNone(text.validate_name("é" * 127))
        for bad in ("", ".", "..", "a/b", "a\0b", "x" * 256, "é" * 128):
            self.assertIsInstance(text.validate_name(bad), str, bad)

    def test_unique_name(self):
        taken = {"a.txt", "a (2).txt", "b", "b - Copy", "c.tar.gz"}
        self.assertEqual(text.unique_name("new.txt", taken.__contains__), "new.txt")
        self.assertEqual(text.unique_name("a.txt", taken.__contains__), "a (3).txt")
        self.assertEqual(text.unique_name("b", taken.__contains__, copy=True), "b - Copy (2)")
        self.assertEqual(text.unique_name("c.tar.gz", taken.__contains__), "c (2).tar.gz")
        self.assertEqual(text.unique_name("a.txt", taken.__contains__, copy=True), "a - Copy.txt")

    def test_unique_name_stays_within_the_limit(self):
        long = "é" * 120 + ".txt"                   # 244 bytes
        result = text.unique_name(long, lambda name: name == long)
        self.assertLessEqual(len(result.encode()), 255)
        self.assertTrue(result.endswith(" (2).txt"))
        result = text.unique_name("x" * 255, lambda name: "(" not in name)
        self.assertEqual(len(result.encode()), 255)

    def test_truncate_never_splits_a_character(self):
        for limit in range(0, 12):
            cut = text.truncate_name("a🙂é\udcffz", limit)
            self.assertLessEqual(len(cut.encode("utf-8", "surrogateescape")), limit)
            self.assertTrue("a🙂é\udcffz".startswith(cut))

    def test_display_name(self):
        self.assertEqual(text.display_name("plain.txt"), "plain.txt")
        self.assertEqual(text.display_name("a\nb\tc"), "a␊b␉c")
        self.assertEqual(text.display_name("bad\udcff"), "bad�")
        self.assertEqual(text.display_name("del\x7f"), "del␡")

    def test_format_size(self):
        cases = {0: "0 bytes", 1: "1 byte", 1023: "1023 bytes", 1024: "1.00 KB", 1536: "1.50 KB",
                 10 * 1024: "10.0 KB", 150 * 1024: "150 KB", 1024 ** 2: "1.00 MB", 5 * 1024 ** 3: "5.00 GB",
                 1024 ** 4: "1.00 TB", -5: "0 bytes", 2 ** 70: "1024 EB"}
        for size, expected in cases.items():
            self.assertEqual(text.format_size(size), expected, size)

    def test_plural(self):
        self.assertEqual(text.plural(1, "item"), "1 item")
        self.assertEqual(text.plural(1204, "item"), "1,204 items")
        self.assertEqual(text.plural(2, "entry", "entries"), "2 entries")


if __name__ == "__main__":
    unittest.main()
