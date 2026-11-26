"""tools/build_emoji.py on a small copy of the Unicode data (standard library only).

Run from packages/boswas-desktop: python3 -B -m unittest discover -s tools/tests
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_emoji  # noqa: E402

EMOJI_TEST = """\
# emoji-test.txt
# Version: 15.1
# group: Smileys & Emotion
# subgroup: face-smiling
1F600                                                  ; fully-qualified     # 😀 E1.0 grinning face
263A FE0F                                              ; fully-qualified     # ☺️ E0.6 smiling face
263A                                                   ; unqualified         # ☺ E0.6 smiling face
# group: People & Body
# subgroup: hand-fingers-open
1F44B                                                  ; fully-qualified     # 👋 E0.6 waving hand
1F44B 1F3FB                                            ; fully-qualified     # 👋🏻 E1.0 waving hand: light skin tone
1F44B 1F3FC                                            ; fully-qualified     # 👋🏼 E1.0 waving hand: medium-light skin tone
1F44B 1F3FD                                            ; fully-qualified     # 👋🏽 E1.0 waving hand: medium skin tone
1F44B 1F3FE                                            ; fully-qualified     # 👋🏾 E1.0 waving hand: medium-dark skin tone
1F44B 1F3FF                                            ; fully-qualified     # 👋🏿 E1.0 waving hand: dark skin tone
1F9D4 200D 2642 FE0F                                   ; fully-qualified     # 🧔‍♂️ E13.1 man: beard
1F9D4 1F3FB 200D 2642 FE0F                             ; fully-qualified     # 🧔🏻‍♂️ E13.1 man: light skin tone, beard
1F91D                                                  ; fully-qualified     # 🤝 E3.0 handshake
1F91D 1F3FB                                            ; fully-qualified     # 🤝🏻 E14.0 handshake: light skin tone
1F91D 1F3FC                                            ; fully-qualified     # 🤝🏼 E14.0 handshake: medium-light skin tone
1F91D 1F3FD                                            ; fully-qualified     # 🤝🏽 E14.0 handshake: medium skin tone
1F91D 1F3FE                                            ; fully-qualified     # 🤝🏾 E14.0 handshake: medium-dark skin tone
1F91D 1F3FF                                            ; fully-qualified     # 🤝🏿 E14.0 handshake: dark skin tone
1FAF1 1F3FB 200D 1FAF2 1F3FC                           ; fully-qualified     # 🫱🏻‍🫲🏼 E14.0 handshake: light skin tone, medium-light skin tone
# group: Component
# subgroup: skin-tone
1F3FB                                                  ; component           # 🏻 E1.0 light skin tone
"""

ANNOTATIONS = """<?xml version="1.0" encoding="UTF-8" ?>
<ldml><annotations>
<annotation cp="😀">face | grin | grinning face</annotation>
<annotation cp="😀" type="tts">grinning face</annotation>
<annotation cp="☺">face | outlined | relaxed | smile | smiling face</annotation>
<annotation cp="€">currency | EUR | euro</annotation>
<annotation cp="€" type="tts">euro</annotation>
</annotations></ldml>
"""

DERIVED = """<?xml version="1.0" encoding="UTF-8" ?>
<ldml><annotations>
<annotation cp="👋">hand | wave | waving</annotation>
</annotations></ldml>
"""

UNICODE_DATA = """\
0041;LATIN CAPITAL LETTER A;Lu;0;L;;;;;N;;;;0061;
00C0;LATIN CAPITAL LETTER A WITH GRAVE;Lu;0;L;0041 0300;;;;N;LATIN CAPITAL LETTER A GRAVE;;;00E0;
0300;COMBINING GRAVE ACCENT;Mn;230;NSM;;;;;N;NON-SPACING GRAVE;;;;
20AC;EURO SIGN;Sc;0;ET;;;;;N;;;;;
"""


def sources(root: Path) -> Path:
    (root / "emoji").mkdir(parents=True)
    (root / "cldr/common/annotations").mkdir(parents=True)
    (root / "cldr/common/annotationsDerived").mkdir(parents=True)
    (root / "cldr/common/dtd").mkdir(parents=True)
    (root / "emoji/emoji-test.txt").write_text(EMOJI_TEST, encoding="utf-8")
    (root / "cldr/common/annotations/en.xml").write_text(ANNOTATIONS, encoding="utf-8")
    (root / "cldr/common/annotationsDerived/en.xml").write_text(DERIVED, encoding="utf-8")
    (root / "cldr/common/dtd/ldml.dtd").write_text('<!ATTLIST version cldrVersion CDATA #FIXED "44" >\n')
    (root / "UnicodeData.txt").write_text(UNICODE_DATA, encoding="utf-8")
    (root / "DerivedAge.txt").write_text("# DerivedAge-15.1.0.txt\n")
    return root


def records(text: str) -> list[list[str]]:
    return [line.split("\t") for line in text.splitlines() if not line.startswith("#")]


class GeneratorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = sources(Path(self.tmp.name) / "unicode")

    def tearDown(self):
        self.tmp.cleanup()

    def test_emoji_come_in_unicode_order_with_cldr_keywords_and_their_skin_tones(self):
        text = build_emoji.build(self.root)
        rows = records(text)
        self.assertEqual(rows[0], ["G", "smileys", "Smileys & emotion", "emoji"])
        self.assertEqual(rows[1], ["😀", "grinning face", "face|grin", ""], "the name is not repeated as a keyword")
        # Fully qualified only, and found in CLDR without its presentation selector.
        self.assertEqual(rows[2], ["☺️", "smiling face", "face|outlined|relaxed|smile", ""])
        self.assertEqual(rows[3], ["G", "people", "People", "emoji"])
        self.assertEqual(rows[4], ["👋", "waving hand", "hand|wave|waving", "👋🏻,👋🏼,👋🏽,👋🏾,👋🏿"])
        # Only some tones in the data: none offered rather than a gap.
        self.assertEqual(rows[5], ["🧔‍♂️", "man: beard", "", ""])
        # Two-tone variants are not offered; the single-tone ones are.
        self.assertEqual(rows[6], ["🤝", "handshake", "", "🤝🏻,🤝🏼,🤝🏽,🤝🏾,🤝🏿"])
        emoji = [r for r in rows if len(r) == 4 and r[0] != "G" and any("\U0001F300" <= c for c in r[0])]
        self.assertFalse(any("skin tone" in r[1] for r in emoji), "no tone variant and no component is an item")
        self.assertIn("emoji 15.1, Unicode 15.1.0, CLDR 44", text.splitlines()[1])

    def test_kaomoji_and_symbols_follow_the_emoji(self):
        rows = records(build_emoji.build(self.root))
        tabs = [r[3] for r in rows if r[0] == "G"]
        self.assertEqual(tabs, sorted(tabs, key=["emoji", "kaomoji", "symbols"].index))
        shrug = next(r for r in rows if r[0] == "¯\\_(ツ)_/¯")
        self.assertEqual(shrug[1:3], ["shrug", "whatever"])
        # A symbol named by CLDR where it has a name, else by UnicodeData.txt;
        # unassigned and combining characters are left out.
        self.assertIn(["€", "euro", "currency|eur", ""], rows)
        self.assertIn(["À", "latin capital letter a with grave", "", ""], rows)
        self.assertFalse(any(r[0] == "̀" for r in rows))
        for row in rows:
            self.assertEqual(len(row), 4, row)

    def test_check_tells_whether_the_committed_file_is_current(self):
        out = Path(self.tmp.name) / "emoji.tsv"
        self.assertEqual(build_emoji.main(["--unicode", str(self.root), "--output", str(out)]), 0)
        self.assertEqual(build_emoji.main(["--check", "--unicode", str(self.root), "--output", str(out)]), 0)
        out.write_text(out.read_text(encoding="utf-8").replace("grinning", "grimacing"), encoding="utf-8")
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(build_emoji.main(["--check", "--unicode", str(self.root), "--output", str(out)]), 1)
        self.assertIn("run tools/build_emoji.py", err.getvalue())
        with redirect_stderr(io.StringIO()) as err:
            code = build_emoji.main(["--check", "--unicode", str(Path(self.tmp.name) / "none"), "--output", str(out)])
        self.assertEqual(code, 2)
        self.assertIn("unicode-cldr-core", err.getvalue())

    def test_the_committed_file_is_current_when_the_sources_are_installed(self):
        if not (build_emoji.DEFAULT_SOURCES / "emoji" / "emoji-test.txt").exists():
            self.skipTest("unicode-data and unicode-cldr-core are not installed")
        self.assertEqual(build_emoji.main(["--check"]), 0)


if __name__ == "__main__":
    unittest.main()
