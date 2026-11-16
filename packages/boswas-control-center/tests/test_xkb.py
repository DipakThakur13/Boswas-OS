"""xkb: the keyboard layout names of the XKB data, and the configuration's layout strings."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures  # noqa: E402,F401  (import paths)

from boswas_control_center import compositorconfig, xkb  # noqa: E402

# The shape of /usr/share/X11/xkb/rules/evdev.lst (xkb-data), shortened.
SAMPLE = """\
! model
  pc86            Generic 86-key PC
  pc105           Generic 105-key PC

! layout
  us              English (US)
  de              German
  in              Indian
  fr              French
  nec_vndr/jp     Japanese (PC-98)
  custom          A user-defined custom Layout

! variant
  dvorak          us: English (Dvorak)
  intl            us: English (US, intl., with dead keys)
  neo             de: German (Neo 2)
  nodeadkeys      de: German (no dead keys)
  eng             in: English (India, with rupee)
  hin-kagapa      in: Hindi (KaGaPa, phonetic)
  bepo            fr: French (BEPO)

! option
  grp                  Switching to another layout
  grp:switch           Right Alt (while pressed)
  grp:alt_shift_toggle Alt+Shift
  grp:caps_toggle      Caps Lock
  grp_led              Use keyboard LED to show alternative layout
  grp_led:scroll       Scroll Lock
  caps:escape          Make Caps Lock an additional Esc
  lv3:ralt_switch      Right Alt
"""


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.catalog = xkb.parse(SAMPLE)

    def test_layouts_variants_and_options(self):
        self.assertEqual(list(self.catalog.layouts), ["us", "de", "in", "fr", "nec_vndr/jp", "custom"])
        self.assertEqual(self.catalog.layouts["de"],
                         xkb.Layout("de", "German", (xkb.Variant("neo", "German (Neo 2)"),
                                                    xkb.Variant("nodeadkeys", "German (no dead keys)"))))
        self.assertEqual(self.catalog.layouts["custom"].variants, ())
        self.assertEqual([option.code for option in self.catalog.options],
                         ["grp:switch", "grp:alt_shift_toggle", "grp:caps_toggle", "grp_led:scroll", "caps:escape",
                          "lv3:ralt_switch"])
        self.assertEqual(self.catalog.switch_options(),
                         [xkb.Option("grp:switch", "Right Alt (while pressed)"),
                          xkb.Option("grp:alt_shift_toggle", "Alt+Shift"), xkb.Option("grp:caps_toggle", "Caps Lock")])
        self.assertEqual(self.catalog.problem, "")

    def test_names(self):
        name = self.catalog.name
        self.assertEqual(name("us"), "English (US)")
        self.assertEqual(name("de", "neo"), "German (Neo 2)")
        self.assertEqual(name("de", "unlisted"), "German (unlisted)")
        self.assertEqual(name("zz"), "zz")
        self.assertEqual(name("zz", "x"), "zz (x)")
        self.assertTrue(self.catalog.known("us") and self.catalog.known("in", "eng"))
        self.assertFalse(self.catalog.known("zz") or self.catalog.known("us", "neo") or self.catalog.known(""))

    def test_choices_are_sorted_by_name(self):
        choices = self.catalog.choices()
        self.assertEqual(len(choices), 6 + 7)
        self.assertEqual([choice.name for choice in choices], sorted((c.name for c in choices), key=str.casefold))
        self.assertIn(xkb.Choice("de", "neo", "German (Neo 2)"), choices)
        self.assertEqual(xkb.Choice("de", "neo", "German (Neo 2)").pair, ("de", "neo"))

    def test_search(self):
        def found(query):
            return [choice.pair for choice in self.catalog.search(query)]
        self.assertEqual(found("german"), [("de", ""), ("de", "neo"), ("de", "nodeadkeys")])
        self.assertEqual(found("GERMAN neo"), [("de", "neo")])
        self.assertEqual(found("de")[0], ("de", ""))                       # the layout code itself comes first
        self.assertEqual(found("us")[0], ("us", ""))
        self.assertEqual(found("dvorak"), [("us", "dvorak")])
        self.assertEqual(found("english"), [("us", ""), ("us", "dvorak"), ("in", "eng"), ("us", "intl")])
        self.assertEqual(found("eng us"), [("us", ""), ("us", "dvorak"), ("us", "intl")])
        self.assertEqual(found("hindi"), [("in", "hin-kagapa")])
        self.assertEqual(found("klingon"), [])
        self.assertEqual(found("   "), [choice.pair for choice in self.catalog.choices()])
        self.assertEqual(found("(")[0:1], found("(")[0:1])                 # punctuation is just text

    def test_hostile_and_odd_lines_are_left_out(self):
        hostile = ("garbage before any section\n"
                   "! layout\n"
                   "  us              English (US)\n"
                   "  us              A second line for us\n"
                   "\tgb\tEnglish (UK)\n"
                   "  nodescription\n"
                   "  bad;code        Shell characters\n"
                   "  a,b             Comma\n"
                   "  " + "x" * 65 + "  Too long a name\n"
                   "  ok" + " " * 2000 + "A line that is far too long\n"
                   "  ctl             Has \x1b[31m control \x00 characters\n"
                   "  rtl             \u202eevil\u202c name\n"
                   "!   VARIANT   \n"
                   "  intl            us: English (intl.)\n"
                   "  intl            us: Again\n"
                   "  orphan          zz: Variant of a layout that is not listed\n"
                   "  nocolon         English without a colon\n"
                   "  empty           us:\n"
                   "  colon           us: Name: with a colon\n"
                   "! unknown section\n"
                   "  xx              Ignored\n"
                   "! option\n"
                   "  grp             Group header\n"
                   "  grp:ok          Fine\n"
                   "  grp:ok          Twice\n"
                   "  grp:a:b         Two colons\n"
                   "  grp:bad name    x\n"
                   "!\n"
                   "  after           An empty section name\n")
        catalog = xkb.parse(hostile)
        self.assertEqual(list(catalog.layouts), ["us", "gb", "ctl", "rtl"])
        self.assertEqual(catalog.layouts["us"].name, "English (US)")
        self.assertEqual(catalog.layouts["gb"].name, "English (UK)")
        self.assertEqual(catalog.layouts["ctl"].name, "Has [31m control characters")
        self.assertEqual(catalog.layouts["rtl"].name, "evil name")
        self.assertEqual(catalog.layouts["us"].variants, (xkb.Variant("intl", "English (intl.)"),
                                                          xkb.Variant("colon", "Name: with a colon")))
        self.assertEqual(catalog.options, (xkb.Option("grp:ok", "Fine"), xkb.Option("grp:bad", "name x")))
        for text in ("", "\n\n", "! layout", "\x00\x01\x02", "!!!!", "! layout\n" + "\n" * 1000):
            self.assertEqual(xkb.parse(text).layouts, {})

    def test_the_number_of_entries_is_bounded(self):
        text = "! layout\n" + "".join(f"  l{n}  Layout {n}\n" for n in range(xkb.MAX_ENTRIES + 50))
        self.assertEqual(len(xkb.parse(text).layouts), xkb.MAX_ENTRIES)

    def test_every_listed_name_can_be_written_to_the_configuration(self):
        for choice in self.catalog.choices():
            layout, variant = xkb.join([choice.pair])
            self.assertEqual(compositorconfig.check_value("keyboard", "layout", layout), layout)
            self.assertEqual(compositorconfig.check_value("keyboard", "variant", variant), variant)
        for option in self.catalog.switch_options():
            self.assertEqual(compositorconfig.check_value("keyboard", "options", option.code), option.code)


class LoadTest(unittest.TestCase):
    def test_missing_unreadable_and_empty_files(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = xkb.load(str(Path(directory, "missing.lst")))
            self.assertEqual(missing.layouts, {})
            self.assertIn("cannot be read", missing.problem)
            Path(directory, "empty.lst").write_text("! layout\n")
            self.assertIn("names no layout", xkb.load(str(Path(directory, "empty.lst"))).problem)
            Path(directory, "binary.lst").write_bytes(b"! layout\n  us  English \xff\xfe (US)\n")
            self.assertEqual(list(xkb.load(str(Path(directory, "binary.lst"))).layouts), ["us"])
            Path(directory, "big.lst").write_bytes(b"! layout\n" + b"# " * xkb.MAX_BYTES)
            self.assertIn("too large", xkb.load(str(Path(directory, "big.lst"))).problem)
            self.assertIn("cannot be read", xkb.load(directory).problem)

    @unittest.skipUnless(Path(xkb.RULES_FILE).exists(), "xkb-data is not installed")
    def test_the_installed_list(self):
        catalog = xkb.load()
        self.assertEqual(catalog.problem, "")
        self.assertGreater(len(catalog.layouts), 80)
        self.assertEqual(catalog.name("us"), "English (US)")
        self.assertEqual(catalog.name("de", "neo"), "German (Neo 2)")
        self.assertIn("grp:alt_shift_toggle", [option.code for option in catalog.switch_options()])
        self.assertEqual(catalog.search("english us")[0].pair, ("us", ""))
        for choice in catalog.choices():                                   # nothing the configuration would refuse
            layout, variant = xkb.join([choice.pair])
            compositorconfig.check_value("keyboard", "layout", layout)
            compositorconfig.check_value("keyboard", "variant", variant)
        for option in catalog.switch_options():
            compositorconfig.check_value("keyboard", "options", option.code)


class StringsTest(unittest.TestCase):
    def test_split(self):
        self.assertEqual(xkb.split("us"), [("us", "")])
        self.assertEqual(xkb.split("us,de", ",neo"), [("us", ""), ("de", "neo")])
        self.assertEqual(xkb.split("us, de ,in", "intl"), [("us", "intl"), ("de", ""), ("in", "")])
        self.assertEqual(xkb.split("us,,de", "a,b,c,d"), [("us", "a"), ("de", "c")])
        self.assertEqual(xkb.split("", "neo"), [])
        self.assertEqual(xkb.split(",,"), [])
        self.assertEqual(xkb.split(None, 5), [])

    def test_join(self):
        self.assertEqual(xkb.join([("us", "")]), ("us", ""))
        self.assertEqual(xkb.join([("us", ""), ("de", "neo"), ("in", "")]), ("us,de,in", ",neo,"))
        self.assertEqual(xkb.join([("us", ""), ("de", "")]), ("us,de", ""))
        for pairs in ([], [("", "")], [("us,de", "")], [("us", "a,b")], [("us", None)], [("u s", "")],
                      [(5, "")], [("us", "x" * 65)]):
            with self.assertRaises(ValueError, msg=pairs):
                xkb.join(pairs)

    def test_round_trip(self):
        for pairs in ([("us", "")], [("us", "intl"), ("de", "")], [("in", "eng"), ("us", ""), ("de", "neo"),
                                                                    ("fr", "bepo")]):
            self.assertEqual(xkb.split(*xkb.join(pairs)), pairs)

    def test_the_switching_option(self):
        self.assertEqual(xkb.switch_option(""), "")
        self.assertEqual(xkb.switch_option("caps:escape, grp:alt_shift_toggle ,grp:caps_toggle"),
                         "grp:alt_shift_toggle")
        self.assertEqual(xkb.switch_option("grp_led:scroll,caps:escape"), "")
        self.assertEqual(xkb.switch_option(None), "")
        self.assertEqual(xkb.with_switch_option("", "grp:alt_shift_toggle"), "grp:alt_shift_toggle")
        self.assertEqual(xkb.with_switch_option("caps:escape,grp:alt_shift_toggle,grp:x,compose:ralt",
                                                "grp:ctrl_shift_toggle"),
                         "caps:escape,compose:ralt,grp:ctrl_shift_toggle")
        self.assertEqual(xkb.with_switch_option("grp:alt_shift_toggle,caps:escape", ""), "caps:escape")
        self.assertEqual(xkb.with_switch_option("grp:alt_shift_toggle", ""), "")
        for bad in ("caps:escape", "grp:a b", "grp:a,grp:b", "grp:"):
            with self.assertRaises(ValueError, msg=bad):
                xkb.with_switch_option("", bad)


if __name__ == "__main__":
    unittest.main()
