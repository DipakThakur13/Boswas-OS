"""Konsole colour schemes and profiles."""

import unittest

import support

COMMAND = "/usr/bin/bash --rcfile /usr/share/boswas/terminal/bashrc"


def colorscheme(p):
    return support.parse_ini(support.PRESETS_DIR / "konsole" / f"Boswas{p['label']}.colorscheme")


def profile(p):
    return support.parse_ini(support.PRESETS_DIR / "konsole" / f"Boswas {p['label']}.profile")


class KonsoleSchemeTest(unittest.TestCase):
    def test_all_colours(self):
        for p in support.presets():
            with self.subTest(preset=p["id"]):
                s = colorscheme(p)
                expected = {"General"}
                for base in ["Background", "Foreground"] + [f"Color{i}" for i in range(8)]:
                    for suffix in ("", "Faint", "Intense"):
                        expected.add(base + suffix)
                self.assertEqual(set(s), expected)
                for name, group in s.items():
                    if name != "General":
                        self.assertEqual(set(group), {"Color"})
                        support.parse_triplet(group["Color"])
                general = s["General"]
                self.assertEqual(general["Description"], p["name"])
                self.assertEqual(general["Opacity"], "1")
                self.assertEqual(general["Blur"], "false")

    def test_matches_the_palette(self):
        for p in support.presets():
            s = colorscheme(p)
            t = p["terminal"]
            with self.subTest(preset=p["id"]):
                self.assertEqual(support.parse_triplet(s["Background"]["Color"]), support.to_rgb(t["background"]))
                self.assertEqual(support.parse_triplet(s["Foreground"]["Color"]), support.to_rgb(t["foreground"]))
                for i in range(8):
                    self.assertEqual(support.parse_triplet(s[f"Color{i}"]["Color"]), support.to_rgb(t["colors"][i]))
                    self.assertEqual(support.parse_triplet(s[f"Color{i}Intense"]["Color"]),
                                     support.to_rgb(t["colors"][i + 8]))

    def test_readable(self):
        for p in support.presets():
            s = colorscheme(p)
            bg = s["Background"]["Color"]
            with self.subTest(preset=p["id"]):
                self.assertGreaterEqual(support.contrast(s["Foreground"]["Color"], bg),
                                        7.0 if p["id"] == "carbon" else 4.5)
                for i in range(1, 7):   # red .. cyan, normal and intense
                    self.assertGreaterEqual(support.contrast(s[f"Color{i}"]["Color"], bg), 3.5, f"Color{i}")
                    self.assertGreaterEqual(support.contrast(s[f"Color{i}Intense"]["Color"], bg), 3.5,
                                            f"Color{i}Intense")


class KonsoleProfileTest(unittest.TestCase):
    def test_profiles(self):
        for p in support.presets():
            with self.subTest(preset=p["id"]):
                s = profile(p)
                self.assertEqual(s["Appearance"]["ColorScheme"], f"Boswas{p['label']}")
                self.assertTrue((support.PRESETS_DIR / "konsole" / f"Boswas{p['label']}.colorscheme").is_file())
                self.assertIn(s["Appearance"]["Font"], ("Hack,11", "Noto Sans Mono,11"))
                self.assertEqual(s["General"], {"Command": COMMAND, "Name": p["name"], "Parent": "FALLBACK/"})
                self.assertEqual(s["Scrolling"], {"HistorySize": "10000"})
                self.assertEqual(s["Terminal Features"], {"BlinkingCursorEnabled": "false"})


if __name__ == "__main__":
    unittest.main()
